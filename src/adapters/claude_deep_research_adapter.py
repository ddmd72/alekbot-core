"""
ClaudeDeepResearchAdapter — DeepResearchPort implementation backed by Anthropic Claude.

Delivery model: AGENT TASK — create_interaction() enqueues a standard agent_execution
Cloud Task targeting ClaudeDeepResearchRunnerAgent, and returns a UUID immediately.
The runner agent executes the full multi-turn Claude loop and self-delivers the result.

No polling, no webhook, no new task_type. Uses the existing agent_execution mechanism.

Tier → model mapping:
  ECO         → claude-haiku-4-5-20251001  (fast + cheap debugging / light tasks)
  BALANCED    → claude-sonnet-4-6          (research quality + cost efficiency)
  PERFORMANCE → claude-opus-4-6            (maximum quality)
"""
import uuid
from typing import Optional

from ..domain.user import PerformanceTier
from ..ports.deep_research_port import DeepResearchPort, RUNNER_INTENT
from ..ports.task_queue import TaskQueue
from ..utils.logger import logger

_RUNNER_AGENT_ID = "claude_deep_research_runner"
_RUNNER_INTENT = RUNNER_INTENT
_TASK_DEADLINE_SECONDS = 1800  # 30 min — covers the longest Claude research loop


class ClaudeDeepResearchAdapter(DeepResearchPort):
    """
    DeepResearchPort backed by Claude — kick-off adapter only.

    create_interaction() enqueues an agent_execution Cloud Task for
    ClaudeDeepResearchRunnerAgent and returns a UUID job_id immediately.
    The runner agent owns the research loop and result delivery.

    get_status() is not used — delivery is direct from the runner agent.
    """

    MODEL_TIERS = {
        PerformanceTier.ECO:         "claude-haiku-4-5-20251001",
        PerformanceTier.BALANCED:    "claude-sonnet-4-6",
        PerformanceTier.PERFORMANCE: "claude-opus-4-6",
    }

    def __init__(
        self,
        task_queue: TaskQueue,
        model_override: Optional[str] = None,
    ) -> None:
        """
        Args:
            task_queue:     Queue for enqueuing agent_execution Cloud Tasks.
            model_override: Pin a specific Claude model regardless of tier.
                            Configure via CLAUDE_DEEP_RESEARCH_MODEL env var in main.py.
        """
        self._task_queue = task_queue
        self._model_override = model_override
        logger.info("✅ [ClaudeDeepResearchAdapter] Initialized")

    def _resolve_model(self, tier: PerformanceTier) -> str:
        return self._model_override or self.MODEL_TIERS[tier]

    async def create_interaction(
        self,
        query: str,
        user_id: str,
        account_id: str,
        original_query: str,
        tier: PerformanceTier = PerformanceTier.BALANCED,
        system_prompt: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> str:
        """
        Enqueue an agent_execution Cloud Task for ClaudeDeepResearchRunnerAgent.

        The resolved model name is passed in the task context so the runner agent
        uses the same tier mapping without re-resolving from user config.
        """
        job_id = str(uuid.uuid4())
        await self._task_queue.enqueue_agent_task(
            agent_id=_RUNNER_AGENT_ID,
            intent=_RUNNER_INTENT,
            query=query,
            context={
                "user_id": user_id,
                "account_id": account_id,
                "original_query": original_query,
                "system_prompt": system_prompt or "",
                "model": self._resolve_model(tier),
                "job_id": job_id,
                "session_id": session_id or "",
            },
            deadline_seconds=_TASK_DEADLINE_SECONDS,
        )
        logger.info("[DeepResearch][claude] Task enqueued: job=%s", job_id[:16])
        return job_id

    async def get_status(self, job_id: str) -> tuple[str, str]:
        """Not used — delivery is direct from the runner agent via notification_service."""
        return "in_progress", ""
