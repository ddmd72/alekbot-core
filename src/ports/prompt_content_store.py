"""
PromptContentStore — capture LLM interactions to a queryable backend.

Implementations:
  BigQueryPromptContentAdapter — one row per interaction in a 30-day-TTL table,
    queryable via SQL and joined to the tracing backend by trace_id.

The adapter owns record building (request/response → row) and trace/user lookup,
so the domain never sees storage shapes — agents hand over their native LLM
objects and identity, nothing else.

``record_turn`` is the hot-path capture: best-effort, non-blocking, non-raising.
It schedules the write in the background and returns immediately, so it adds no
latency to the LLM call and a storage failure never breaks a user request.
Durable capture methods (for expensive deep-research outputs) are added separately.
"""

from abc import ABC, abstractmethod
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..domain.llm import LLMRequest, LLMResponse


class PromptContentStore(ABC):
    """Capture LLM request/response content for later querying."""

    @abstractmethod
    async def record_turn(
        self,
        *,
        request: "LLMRequest",
        response: "LLMResponse",
        agent_id: str,
        agent_type: str,
        account_id: Optional[str],
        turn: int,
        latency_ms: float,
        provider: str,
    ) -> None:
        """Capture one LLM request/response turn.

        Best-effort hot-path contract: MUST NOT raise, MUST NOT block. The
        implementation builds the record (pulling trace_id/user_id from the
        request-scoped telemetry context), schedules the write in the
        background, and returns. A failed write is logged, never propagated.
        """
        ...

    @abstractmethod
    async def record_dr_result(
        self,
        *,
        output_text: str,
        query: str,
        user_id: Optional[str],
        account_id: Optional[str],
        model: str,
        provider: str,
        source: str,
        job_id: Optional[str] = None,
        pass_index: Optional[int] = None,
        total_tokens: int = 0,
    ) -> None:
        """Durably capture an expensive deep-research result.

        Unlike record_turn, this is **awaited and retried** — deep research is
        costly and its output must survive even if downstream delivery fails, so
        callers write it BEFORE delivering. Still non-raising: exhausted retries
        log an error and return (delivery proceeds regardless).

        ``pass_index`` tags the pass for multi-pass research (1 = first/round-1,
        2 = critic/final); None for single-pass. ``source`` records the origin
        (e.g. "claude_job", "openai_webhook").
        """
        ...
