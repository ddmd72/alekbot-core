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
"""
import base64
from typing import List

from openai import AsyncOpenAI
from openai.types.images_response import ImagesResponse

from ..ports.image_generation_port import GeneratedImage, ImageGenerationPort
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
            # layer, where NO_RETRY_POLICY actually applies. 120.0s is a starting
            # value, not a measured one — no latency data yet for
            # grok-imagine-image-2.0 (RFC §10 open question #3); revisit after first
            # live measurements, same caveat as agent_config.py's request_timeout_s.
            timeout=120.0,
            max_retries=0,
        )

    async def generate(
        self, prompt: str, *, aspect_ratio: str = "auto", n: int = 1
    ) -> List[GeneratedImage]:
        try:
            response = await self._client.images.generate(
                model=_MODEL,
                prompt=prompt,
                n=n,
                response_format="b64_json",
                extra_body={"aspect_ratio": aspect_ratio},
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

    async def edit(
        self, prompt: str, reference_images: List[bytes], *, mime_type: str = "image/png"
    ) -> GeneratedImage:
        # xAI's JSON shape for a reference image: {"url": <data-uri-or-real-url>,
        # "type": "image_url"} — same wrapper for a base64 data URI as for a real
        # public URL (confirmed against docs.x.ai/developers/model-capabilities/
        # images/editing). Multi-image (up to 3, RFC §10 open question #2) is out
        # of scope for v1 — single reference image only (RFC §6 decision #6).
        data_uri = f"data:{mime_type};base64,{base64.b64encode(reference_images[0]).decode('ascii')}"

        response = await self._client.post(
            "/images/edits",
            cast_to=ImagesResponse,
            body={
                "model": _MODEL,
                "prompt": prompt,
                "image": {"url": data_uri, "type": "image_url"},
                "response_format": "b64_json",
            },
        )
        item = response.data[0]
        return GeneratedImage(data=base64.b64decode(item.b64_json), mime_type="image/png")
