"""
ClaudeDeepResearchAdapter — DeepResearchPort implementation backed by Anthropic Claude.

Delivery model: CLOUD RUN JOB — create_interaction() triggers a Cloud Run Job execution
via JobRunnerPort and returns a UUID immediately. The job runs independently with no
Cloud Tasks deadline (task-timeout configured at job level, up to 168 hours).

The job (job_main.py) owns the full research loop (ClaudeDeepResearchRunnerAgent)
and result delivery (DocPlanner Cloud Task → DOCX → user notification).

Tier → model mapping:
  ECO / TIER1-3 → claude-haiku-4-5-20251001  (fast + cheap debugging / light tasks)
  BALANCED      → claude-sonnet-4-6           (research quality + cost efficiency)
  PERFORMANCE   → claude-sonnet-4-6           (same as BALANCED for deep research)
  ULTRA         → claude-opus-4-8             (maximum quality; upgraded from 4-7 2026-05-30)
"""
import json
import uuid
from typing import Optional

from ..domain.user import PerformanceTier
from ..ports.deep_research_port import DeepResearchPort
from ..ports.job_runner_port import JobRunnerPort
from ..utils.logger import logger


class ClaudeDeepResearchAdapter(DeepResearchPort):
    """
    DeepResearchPort backed by Claude — kick-off adapter only.

    create_interaction() triggers a Cloud Run Job execution for
    ClaudeDeepResearchRunnerAgent and returns a UUID job_id immediately.
    The job owns the research loop and result delivery.

    get_status() is not used — delivery is direct from the job.
    """

    MODEL_TIERS = {
        PerformanceTier.ECO:         "claude-haiku-4-5-20251001",
        PerformanceTier.BALANCED:    "claude-sonnet-4-6",
        PerformanceTier.PERFORMANCE: "claude-sonnet-4-6",
        PerformanceTier.ULTRA:       "claude-opus-4-8",
        PerformanceTier.TIER1:       "claude-haiku-4-5-20251001",
        PerformanceTier.TIER2:       "claude-haiku-4-5-20251001",
        PerformanceTier.TIER3:       "claude-haiku-4-5-20251001",
    }

    def __init__(
        self,
        job_runner: JobRunnerPort,
        job_name: str,
        model_override: Optional[str] = None,
    ) -> None:
        """
        Args:
            job_runner:     Port for triggering Cloud Run Job executions.
            job_name:       Cloud Run Job name (e.g. "alek-research-job-dev").
            model_override: Pin a specific Claude model regardless of tier.
                            Configure via CLAUDE_DEEP_RESEARCH_MODEL env var in main.py.
        """
        self._job_runner = job_runner
        self._job_name = job_name
        self._model_override = model_override
        logger.info(
            "✅ [ClaudeDeepResearchAdapter] Initialized: job=%s", job_name
        )

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
        second_pass: bool = False,
    ) -> str:
        """
        Trigger a Cloud Run Job execution for ClaudeDeepResearchRunnerAgent.

        The query and context are passed as env var overrides to the job container.
        The resolved model name is included so the job uses the same tier mapping
        without re-resolving from user config.
        """
        job_id = str(uuid.uuid4())
        context = {
            "user_id": user_id,
            "account_id": account_id,
            "original_query": original_query,
            "system_prompt": system_prompt or "",
            "model": self._resolve_model(tier),
            "job_id": job_id,
            "session_id": session_id or "",
            "second_pass": second_pass,
        }
        await self._job_runner.run_job(
            job_name=self._job_name,
            env_overrides={
                "JOB_QUERY": query,
                "JOB_CONTEXT_JSON": json.dumps(context),
            },
        )
        logger.info("[DeepResearch][claude] Job triggered: job=%s", job_id[:16])
        return job_id

    async def get_status(self, job_id: str) -> tuple[str, str]:
        """Not used — delivery is direct from the job via notification_service."""
        return "in_progress", ""
