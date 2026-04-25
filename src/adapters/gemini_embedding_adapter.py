from google import genai
from google.genai import errors as genai_errors
import asyncio
import os
from typing import List
from ..ports.embedding_service import EmbeddingService
from ..utils.logger import logger

# Concurrency cap on in-flight embedding requests.
# Sized for AI Studio Tier 2 (gemini-embedding-001 = 5000 RPM = 83 RPS sustained).
# Targeting ~33% of sustained ceiling: N=20 × ~0.7s avg latency ≈ 28 RPS = 1700 RPM,
# leaving ~3300 RPM headroom for bursts, fact-storage paths, and Cloud Run multi-instance.
# Override via GEMINI_EMBED_CONCURRENCY env var if tier or workload changes.
_DEFAULT_CONCURRENCY = 20
_MAX_RETRIES = 3
_INITIAL_BACKOFF_SEC = 2.0


class GeminiEmbeddingAdapter(EmbeddingService):
    """Adapter: Gemini-specific embedding implementation."""

    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("GEMINI_API_KEY is required for GeminiEmbeddingAdapter")
        self.api_key = api_key
        self.client = genai.Client(api_key=api_key)
        concurrency = int(os.getenv("GEMINI_EMBED_CONCURRENCY", str(_DEFAULT_CONCURRENCY)))
        self._semaphore = asyncio.Semaphore(concurrency)
        logger.info(f"[GeminiEmbedding] concurrency cap = {concurrency}")

    async def _embed_with_throttle(self, contents, task_type: str):
        """Throttled call with retry on 429 RESOURCE_EXHAUSTED."""
        async with self._semaphore:
            backoff = _INITIAL_BACKOFF_SEC
            for attempt in range(1, _MAX_RETRIES + 1):
                try:
                    return await asyncio.to_thread(
                        self.client.models.embed_content,
                        model="models/gemini-embedding-001",
                        contents=contents,
                        config={
                            "task_type": task_type,
                            "output_dimensionality": 768,
                        },
                    )
                except genai_errors.ClientError as exc:
                    is_rate_limit = getattr(exc, "code", None) == 429 or "RESOURCE_EXHAUSTED" in str(exc)
                    if not is_rate_limit or attempt == _MAX_RETRIES:
                        raise
                    logger.warning(
                        f"[GeminiEmbedding] 429 on attempt {attempt}/{_MAX_RETRIES}, "
                        f"backing off {backoff:.1f}s"
                    )
                    await asyncio.sleep(backoff)
                    backoff *= 2

    async def get_embedding(
        self,
        text: str,
        task_type: str = "RETRIEVAL_DOCUMENT",
    ) -> List[float]:
        """Generate 768-dimensional embedding via Gemini API."""
        result = await self._embed_with_throttle(text, task_type)
        return result.embeddings[0].values

    async def get_embeddings_batch(
        self,
        texts: List[str],
        task_type: str = "RETRIEVAL_DOCUMENT",
    ) -> List[List[float]]:
        """Generate 768-dimensional embeddings for multiple texts in a single API call."""
        result = await self._embed_with_throttle(texts, task_type)
        return [e.values for e in result.embeddings]
