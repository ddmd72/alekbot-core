"""
GeminiDeepResearchAdapter — DeepResearchPort implementation backed by Gemini Deep Research API.

Pure API client — no Cloud Task or queue logic.
Polling orchestration lives in WorkerHandler via enqueue_deep_research_polling.

Uses google.genai interactions client (background=True).
All SDK calls are synchronous — wrapped in run_in_executor to avoid blocking the event loop.

Model: deep-research-pro-preview-12-2025
API ref: https://ai.google.dev/gemini-api/docs/deep-research

# ── CONNECTION LIFECYCLE ──────────────────────────────────────────────────────
#
# PROBLEM: google.genai.Client wraps httpx with default keepalive settings.
# After ~50 minutes of idle, the underlying TCP connection goes stale and Gemini
# returns HTTP 500 instead of a proper error. This is NOT documented by Google.
#
# We observed this empirically: polling every 30 s, failures start at attempt ~102
# (102 × 30 s ≈ 51 min). A fresh genai.Client always recovers immediately.
#
# TWO-LAYER DEFENCE (belt + suspenders):
#
#   Layer 1 — PROACTIVE: recreate the client every _CLIENT_MAX_AGE_SECONDS (20 min).
#             20 min is well within the observed ~50 min window; conservative enough
#             that even an undocumented Google-side change to 30 min wouldn't hurt us.
#
#   Layer 2 — REACTIVE: if get_status() still raises on a fresh client,
#             recreate once more and retry immediately. Catches any one-off blip that
#             slips through Layer 1, without masking persistent real server errors
#             (WorkerHandler's consecutive_errors counter handles those).
#
# If Google ever fixes the SDK or documents connection lifecycle, remove both layers
# and go back to a single long-lived client passed in from the outside.
# ─────────────────────────────────────────────────────────────────────────────
"""
import asyncio
import time
from typing import Optional
from google import genai

from ..domain.user import PerformanceTier
from ..ports.deep_research_port import DeepResearchPort
from ..ports.task_queue import TaskQueue
from ..utils.logger import logger


# Layer 1: recreate the client after this many seconds of age (not idle — age).
# 20 min chosen as a conservative buffer below the observed ~50 min stale threshold.
_CLIENT_MAX_AGE_SECONDS = 20 * 60


class GeminiDeepResearchAdapter(DeepResearchPort):

    MODEL_TIERS = {
        PerformanceTier.ECO:         "deep-research-pro-preview-12-2025",
        PerformanceTier.BALANCED:    "deep-research-pro-preview-12-2025",
        PerformanceTier.PERFORMANCE: "deep-research-pro-preview-12-2025",
        PerformanceTier.ULTRA:       "deep-research-pro-preview-12-2025",
        PerformanceTier.TIER1:       "deep-research-pro-preview-12-2025",
        PerformanceTier.TIER2:       "deep-research-pro-preview-12-2025",
        PerformanceTier.TIER3:       "deep-research-pro-preview-12-2025",
    }

    def __init__(
        self,
        api_key: str,
        task_queue: Optional[TaskQueue] = None,
        model_override: Optional[str] = None,
    ) -> None:
        """
        Args:
            api_key:        Gemini API key. The adapter manages its own genai.Client
                            lifecycle internally — see module-level CONNECTION LIFECYCLE note.
            task_queue:     Queue used to enqueue deep_research_polling Cloud Tasks after
                            each create_interaction() call. None when the queue is not wired
                            (e.g. local dev without Cloud Tasks emulator) — create_interaction()
                            will raise before touching the Gemini API.
            model_override: Pin a specific model identifier regardless of tier.
                            Takes precedence over MODEL_TIERS mapping.
                            Configure via GEMINI_DEEP_RESEARCH_MODEL env var in main.py.
        """
        self._api_key = api_key
        self._task_queue = task_queue
        self._model_override = model_override
        self._client = genai.Client(api_key=api_key)
        self._client_created_at = time.monotonic()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _recreate_client(self) -> None:
        """Discard the current genai.Client and create a fresh one."""
        self._client = genai.Client(api_key=self._api_key)
        self._client_created_at = time.monotonic()
        logger.info("[DeepResearch] genai.Client recreated (fresh HTTP session)")

    def _maybe_refresh_client(self) -> None:
        """Layer 1: proactively recreate if client has exceeded max age."""
        age = time.monotonic() - self._client_created_at
        if age >= _CLIENT_MAX_AGE_SECONDS:
            logger.info(
                f"[DeepResearch] Client age {age:.0f}s >= {_CLIENT_MAX_AGE_SECONDS}s — "
                "proactive recreation to avoid stale HTTP connection"
            )
            self._recreate_client()

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
        """Submit job and enqueue polling Cloud Task. Adapter owns delivery."""
        if self._task_queue is None:
            raise RuntimeError(
                "Deep Research polling unavailable: task queue not configured "
                "(Socket Mode / local dev)."
            )
        resolved_model = self._resolve_model(tier)
        self._maybe_refresh_client()
        loop = asyncio.get_event_loop()
        interaction = await loop.run_in_executor(
            None,
            lambda: self._client.interactions.create(
                input=query,
                agent=resolved_model,
                background=True,
            ),
        )
        logger.info(f"[DeepResearch][gemini] Job submitted: {interaction.id[:16]}")
        await self._task_queue.enqueue_deep_research_polling(
            interaction_id=interaction.id,
            user_id=user_id,
            account_id=account_id,
            query=original_query,
            provider="gemini",
            session_id=session_id or "",
        )
        return interaction.id

    async def get_status(self, job_id: str) -> tuple[str, str]:
        """Poll job status. Returns (status, result_text)."""
        # Layer 1: proactive age check before every poll.
        self._maybe_refresh_client()

        loop = asyncio.get_event_loop()
        try:
            interaction = await loop.run_in_executor(
                None,
                lambda: self._client.interactions.get(job_id),
            )
        except Exception as exc:
            # Layer 2: reactive — recreate client and retry ONCE.
            # Handles the case where a stale connection slips through Layer 1.
            # One retry is safe: if the server is genuinely down, the second call
            # will also fail and WorkerHandler's consecutive_errors counter will catch it.
            logger.warning(
                f"[DeepResearch][gemini] interactions.get failed ({exc}); "
                "recreating client and retrying once"
            )
            self._recreate_client()
            interaction = await loop.run_in_executor(
                None,
                lambda: self._client.interactions.get(job_id),
            )

        status = interaction.status  # "in_progress" | "completed" | "failed"

        if status == "completed":
            result_text = interaction.outputs[-1].text if interaction.outputs else ""
            return "completed", result_text

        if status == "failed":
            error = getattr(interaction, "error", "unknown error")
            return "failed", str(error)

        return "in_progress", ""
