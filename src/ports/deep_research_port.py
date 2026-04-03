"""
DeepResearchPort — port for background deep research job execution.

Each adapter owns its delivery mechanism entirely:
  GeminiDeepResearchAdapter  — submits job, enqueues Cloud Task for polling
  OpenAIDeepResearchAdapter  — submits job with webhook_url; OpenAI pushes result
  ClaudeDeepResearchAdapter  — enqueues agent_execution Cloud Task for ClaudeDeepResearchRunnerAgent

DeepResearchAgent is delivery-agnostic: it calls create_interaction() and returns ACK.
The adapter decides whether polling, webhook, or agent task is used — no queue logic in the agent.
"""

# Stable contract between ClaudeDeepResearchAdapter and ClaudeDeepResearchRunnerAgent.
# Defined here so adapters can import it without violating the adapters → infrastructure rule.
RUNNER_INTENT = "execute_deep_research_claude"
from abc import ABC, abstractmethod
from typing import Optional

from ..domain.user import PerformanceTier


class DeepResearchPort(ABC):
    """Port for submitting long-running deep research jobs."""

    @abstractmethod
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
        Submit a deep research job and arrange for result delivery.

        Delivery mechanism is adapter-specific:
          GeminiDeepResearchAdapter → enqueues deep_research_polling Cloud Task.
          OpenAIDeepResearchAdapter → embeds user_id/account_id in OpenAI metadata;
                                      OpenAI echoes them back in the webhook payload.

        Args:
            query:          Full research brief (with language instruction appended).
            user_id:        User identifier — for delivery routing.
            account_id:     Account identifier — for delivery routing.
            original_query: Bare research brief without language suffix — for context.
            tier:           Performance tier — adapter maps internally to provider model.
            system_prompt:  Optional assembled prompt from PromptBuilder.
                            Adapters that do not use a system prompt may ignore this.
                            Future Claude-backed adapter will use it as a system block.

        Returns:
            job_id: Opaque string identifying the submitted job.
        """

    @abstractmethod
    async def get_status(self, job_id: str) -> tuple[str, str]:
        """
        Poll the status of a running job.

        Primary path: GeminiDeepResearchAdapter (polling loop in WorkerHandler).
        Emergency fallback: OpenAIDeepResearchAdapter (primary delivery is webhook).

        Returns:
            (status, payload) where:
              "in_progress" → payload is ""
              "completed"   → payload is the full result text
              "failed"      → payload is the error message string
        """

