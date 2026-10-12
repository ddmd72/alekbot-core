from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
import asyncio
import httpx
import os
from typing import List
from ..domain.exceptions import (
    LLMClientError,
    LLMRateLimitError,
    LLMServerError,
    LLMUnavailableError,
    TRANSIENT_RETRY_TYPES,
)
from ..domain.retry_policy import DEFAULT_RETRY_POLICY
from ..ports.embedding_service import EmbeddingService
from ..utils.logger import logger
from ..utils.retry import retry_async

# Concurrency cap on in-flight embedding requests.
# Sized for AI Studio Tier 2 (5000 RPM = 83 RPS sustained).
# Targeting ~33% of sustained ceiling: N=20 × ~0.7s avg latency ≈ 28 RPS = 1700 RPM,
# leaving ~3300 RPM headroom for bursts, fact-storage paths, and Cloud Run multi-instance.
# Override via GEMINI_EMBED_CONCURRENCY env var if tier or workload changes.
_DEFAULT_CONCURRENCY = 20

_MODEL = "gemini-embedding-2"
_OUTPUT_DIM = 768  # Matryoshka truncation from native 3072; preserves existing Firestore indexes.
# google-genai sends with no HTTP timeout by default, so a hung connection held its worker thread forever.
# Milliseconds (HttpOptions.timeout); applied to connect, read, write and pool alike.
_HTTP_TIMEOUT_MS = 30_000

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

    # The vector space every embedding of this adapter lives in. Composition injects it into the
    # drive index, which stamps it on each card (USER_DRIVE_INDEXING_RFC §12.3; plan Δ1: a class
    # attribute, not a port member). A model, dimension or task-prefix (_TASK_PREFIXES) change alters
    # every vector and must change this string.
    EMBEDDING_SPACE = f"{_MODEL}/{_OUTPUT_DIM}"

    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("GEMINI_API_KEY is required for GeminiEmbeddingAdapter")
        self.api_key = api_key
        self.client = genai.Client(api_key=api_key,
                                   http_options=genai_types.HttpOptions(timeout=_HTTP_TIMEOUT_MS))
        concurrency = int(os.getenv("GEMINI_EMBED_CONCURRENCY", str(_DEFAULT_CONCURRENCY)))
        self._semaphore = asyncio.Semaphore(concurrency)
        logger.info(f"[GeminiEmbedding] concurrency cap = {concurrency}, model = {_MODEL}")

    async def _embed_with_throttle(self, contents):
        """Throttled embedding call.

        Maps genai SDK errors to the shared typed taxonomy, then retries transient
        ones (429/503) via the shared executor under the same RetryPolicy as the LLM
        path. Per-item granularity (called once per text) is why retry lives here and
        not in a port-boundary proxy: get_embeddings_batch fans out over this method,
        so a single text's 503 must not re-run the whole batch.
        """
        async def _call():
            try:
                return await asyncio.to_thread(
                    self.client.models.embed_content,
                    model=_MODEL,
                    contents=contents,
                    config={"output_dimensionality": _OUTPUT_DIM},
                )
            except genai_errors.ClientError as exc:
                code = getattr(exc, "code", None)
                if code == 429 or "RESOURCE_EXHAUSTED" in str(exc):
                    raise LLMRateLimitError(str(exc), http_status=429) from exc
                # Other 4xx — deterministic, not retried. Typed with its status so a caller can tell a
                # per-input refusal (400) from a config failure (401/403/404) without importing the SDK.
                raise LLMClientError(str(exc), http_status=code) from exc
            except genai_errors.ServerError as exc:
                code = getattr(exc, "code", None)
                if code == 503:
                    raise LLMUnavailableError(str(exc), http_status=503) from exc
                if isinstance(code, int) and 500 <= code < 600:
                    # 500/502/504 — not in the transient retry set (matches the LLM
                    # path); propagates so the caller degrades gracefully.
                    raise LLMServerError(str(exc), http_status=code) from exc
                raise
            except httpx.TimeoutException as exc:
                # No round trip completed: the same transient class as a 503, so it is retried here and the
                # drive tick stops as systemic (USER_DRIVE_INDEXING_RFC §8.2).
                raise LLMUnavailableError(f"Embedding request timed out ({type(exc).__name__})") from exc

        async with self._semaphore:
            return await retry_async(
                _call,
                policy=DEFAULT_RETRY_POLICY,
                retryable=tuple(TRANSIENT_RETRY_TYPES),
                on_retry=lambda e, attempt, backoff: logger.warning(
                    f"[GeminiEmbedding] transient {type(e).__name__} "
                    f"(http_status={getattr(e, 'http_status', None)}) on attempt "
                    f"{attempt}, backing off {backoff:.1f}s"
                ),
            )

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
