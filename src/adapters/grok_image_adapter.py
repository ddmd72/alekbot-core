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
"""
import base64
import io
from typing import List

from openai import AsyncOpenAI

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
        # NOTE: field name "image" for the reference image bytes matches the
        # standard OpenAI images.edit() SDK signature. xAI's docs confirm the
        # /v1/images/edits endpoint accepts base64 data — the exact SDK-level
        # kwarg for >1 reference image is unconfirmed (RFC §10, open question #2)
        # but irrelevant here: v1 scope is a single reference image only
        # (RFC §6 decision #6).
        # Extract file extension from mime_type (e.g., "image/jpeg" -> "jpg")
        ext = mime_type.split("/")[-1] if "/" in mime_type else "png"
        # Map common mime_type subtype to extension (e.g., "jpeg" -> "jpg")
        ext = "jpg" if ext == "jpeg" else ext
        filename = f"reference_image.{ext}"

        response = await self._client.images.edit(
            model=_MODEL,
            prompt=prompt,
            image=(filename, io.BytesIO(reference_images[0]), mime_type),
            response_format="b64_json",
        )
        item = response.data[0]
        return GeneratedImage(data=base64.b64decode(item.b64_json), mime_type="image/png")
