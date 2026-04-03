"""
OpenAIDeepResearchAdapter — DeepResearchPort implementation backed by OpenAI Responses API.

Pure API client — no Cloud Task or queue logic.
Delivery model: WEBHOOK — OpenAI POSTs to /webhooks/openai/deep-research on completion.
No polling Cloud Tasks needed; get_status() is an emergency fallback only.

user_id / account_id / original_query embedded as OpenAI metadata and echoed back
in the webhook payload so the webhook handler can route delivery without extra storage.

Tier → model mapping:
  ECO / BALANCED  → o4-mini-deep-research-2025-06-26  (faster, cheaper)
  PERFORMANCE     → o3-deep-research-2025-06-26        (higher quality)
"""
from typing import Optional
from openai import AsyncOpenAI

from ..domain.user import PerformanceTier
from ..ports.deep_research_port import DeepResearchPort
from ..utils.logger import logger


class OpenAIDeepResearchAdapter(DeepResearchPort):
    """
    DeepResearchPort backed by OpenAI Responses API (background + webhook mode).

    Usage is delivery-agnostic from the caller's perspective:
    DeepResearchAgent calls create_interaction() and receives a job_id.
    Result delivery happens via webhook — no Cloud Task polling needed.
    """

    MODEL_TIERS = {
        PerformanceTier.ECO:         "o4-mini-deep-research-2025-06-26",
        PerformanceTier.BALANCED:    "o4-mini-deep-research-2025-06-26",
        PerformanceTier.PERFORMANCE: "o3-deep-research-2025-06-26",
    }

    def __init__(
        self,
        api_key: str,
        webhook_url: Optional[str] = None,
        model_override: Optional[str] = None,
    ) -> None:
        """
        Args:
            api_key:        OpenAI API key.
            webhook_url:    URL OpenAI will POST to when the job completes.
                            None in local dev / Socket Mode — create_interaction() will
                            log a warning and submit without webhook (result won't be delivered).
            model_override: Pin a specific model identifier regardless of tier.
                            Takes precedence over MODEL_TIERS mapping.
                            Configure via OPENAI_DEEP_RESEARCH_MODEL env var in main.py.
        """
        self._client = AsyncOpenAI(api_key=api_key)
        self._webhook_url = webhook_url
        self._model_override = model_override
        if not webhook_url:
            logger.warning(
                "[OpenAIDeepResearch] No webhook_url configured — "
                "results will not be delivered automatically (local dev mode)"
            )
        logger.info("✅ [OpenAIDeepResearchAdapter] Initialized (webhook_url=%s)", webhook_url)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_model(self, tier: PerformanceTier) -> str:
        """Resolve model: construction-time override wins, then tier mapping."""
        return self._model_override or self.MODEL_TIERS[tier]

    # ------------------------------------------------------------------
    # DeepResearchPort implementation
    # ------------------------------------------------------------------

    async def create_interaction(
        self,
        query: str,
        user_id: str,
        account_id: str,
        original_query: str,
        tier: PerformanceTier = PerformanceTier.BALANCED,
        system_prompt: Optional[str] = None,
        session_id: Optional[str] = None,
        second_pass: bool = False,
    ) -> str:
        """
        Submit job with webhook delivery. Adapter owns result routing via metadata.

        OpenAI echoes metadata back in the webhook payload — no extra storage needed.
        system_prompt is accepted for interface parity but not used by the OpenAI
        Responses API in this mode. A future adapter (e.g. Claude) may use it.
        """
        resolved_model = self._resolve_model(tier)
        logger.info(
            "[DeepResearch][openai] Submitting job: model=%s query_len=%d",
            resolved_model, len(query),
        )
        # Webhook delivery is configured globally in the OpenAI Dashboard —
        # no per-request webhook parameter exists in the Responses API.
        # OpenAI pushes completion events to the Dashboard-registered URL automatically.
        # self._webhook_url is stored for documentation/validation only.
        response = await self._client.responses.create(
            model=resolved_model,
            input=query,
            background=True,
            tools=[{"type": "web_search_preview"}],
            metadata={
                "user_id": user_id,
                "account_id": account_id,
                "query": original_query[:512],
                "session_id": session_id or "",
            },
        )
        logger.info("[DeepResearch][openai] Job submitted: %s", response.id[:16])
        return response.id

    async def get_status(self, job_id: str) -> tuple[str, str]:
        """
        Poll job status — emergency fallback only. Primary delivery is webhook.

        Called by WorkerHandler only if a polling Cloud Task was somehow enqueued
        for an OpenAI job (should not happen in normal operation).
        """
        response = await self._client.responses.retrieve(job_id)
        status = response.status

        if status == "completed":
            return "completed", response.output_text or ""

        if status == "failed":
            error = getattr(response, "error", None)
            return "failed", str(error) if error else "unknown error"

        return "in_progress", ""
