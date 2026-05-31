from google import genai
from google.genai import errors as genai_errors
import asyncio
import os
from typing import List
from ..ports.embedding_service import EmbeddingService
from ..utils.logger import logger

# Concurrency cap on in-flight embedding requests.
# Sized for AI Studio Tier 2 (5000 RPM = 83 RPS sustained).
# Targeting ~33% of sustained ceiling: N=20 × ~0.7s avg latency ≈ 28 RPS = 1700 RPM,
# leaving ~3300 RPM headroom for bursts, fact-storage paths, and Cloud Run multi-instance.
# Override via GEMINI_EMBED_CONCURRENCY env var if tier or workload changes.
_DEFAULT_CONCURRENCY = 20
_MAX_RETRIES = 3
_INITIAL_BACKOFF_SEC = 2.0

_MODEL = "gemini-embedding-2"
_OUTPUT_DIM = 768  # Matryoshka truncation from native 3072; preserves existing Firestore indexes.

# v2 protocol: task_type is no longer a config parameter. Task is signalled by an
# instruction prefix on the input text. The public adapter API still accepts task_type
# for caller-side stability; translation happens here. See:
# docs/04_solution_strategy/decisions/embedding_model_migration_v1_to_v2.md
_TASK_PREFIXES = {
    "RETRIEVAL_DOCUMENT":  "title: | text: {text}",
    "RETRIEVAL_QUERY":     "task: search result | query: {text}",
    "SEMANTIC_SIMILARITY": "{text}",
}


def _apply_task_prefix(text: str, task_type: str) -> str:
    """Translate legacy v1 task_type into a v2 instruction-prefixed input string."""
    template = _TASK_PREFIXES.get(task_type)
    if template is None:
        raise ValueError(
            f"Unsupported task_type for gemini-embedding-2: {task_type!r}. "
            f"Supported: {sorted(_TASK_PREFIXES)}."
        )
    return template.format(text=text)


class GeminiEmbeddingAdapter(EmbeddingService):
    """Adapter: Gemini-specific embedding implementation (v2 model, 768-dim)."""

    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("GEMINI_API_KEY is required for GeminiEmbeddingAdapter")
        self.api_key = api_key
        self.client = genai.Client(api_key=api_key)
        concurrency = int(os.getenv("GEMINI_EMBED_CONCURRENCY", str(_DEFAULT_CONCURRENCY)))
        self._semaphore = asyncio.Semaphore(concurrency)
        logger.info(f"[GeminiEmbedding] concurrency cap = {concurrency}, model = {_MODEL}")

    async def _embed_with_throttle(self, contents):
        """Throttled call with retry on transient failures (429 rate limit, 5xx server)."""
        async with self._semaphore:
            backoff = _INITIAL_BACKOFF_SEC
            for attempt in range(1, _MAX_RETRIES + 1):
                try:
                    return await asyncio.to_thread(
                        self.client.models.embed_content,
                        model=_MODEL,
                        contents=contents,
                        config={"output_dimensionality": _OUTPUT_DIM},
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
                except genai_errors.ServerError as exc:
                    # 5xx (503 UNAVAILABLE / 500 / 504) — transient server-side failure,
                    # same retry class as 429. The embedding endpoint 503s under load;
                    # without this the error propagated and degraded fact search to [].
                    if attempt == _MAX_RETRIES:
                        raise
                    logger.warning(
                        f"[GeminiEmbedding] server error (code={getattr(exc, 'code', None)}) "
                        f"on attempt {attempt}/{_MAX_RETRIES}, backing off {backoff:.1f}s"
                    )
                    await asyncio.sleep(backoff)
                    backoff *= 2

    async def get_embedding(
        self,
        text: str,
        task_type: str = "RETRIEVAL_DOCUMENT",
    ) -> List[float]:
        """Generate a 768-dimensional embedding via gemini-embedding-2."""
        prefixed = _apply_task_prefix(text, task_type)
        result = await self._embed_with_throttle(prefixed)
        return result.embeddings[0].values

    async def get_embeddings_batch(
        self,
        texts: List[str],
        task_type: str = "RETRIEVAL_DOCUMENT",
    ) -> List[List[float]]:
        """Generate 768-dimensional embeddings for multiple texts.

        v2 API note: gemini-embedding-2 treats `contents=List[str]` as multimodal
        parts of ONE document (returning a single embedding) rather than as a true
        batch. To get N embeddings for N texts we issue N parallel single-content
        calls; the existing semaphore caps in-flight calls so this is a no-op for
        throughput vs the previous v1 batch endpoint at our batch sizes (3–4 texts).
        """
        # Validate every task_type up front so unknown types fail before any API call.
        for t in texts:
            _apply_task_prefix(t, task_type)
        return list(await asyncio.gather(
            *[self.get_embedding(t, task_type) for t in texts]
        ))
