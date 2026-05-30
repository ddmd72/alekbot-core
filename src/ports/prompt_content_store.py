"""
PromptContentStore — persist full LLM request/response content for querying.

Implementations:
  BigQueryPromptContentAdapter — one row per LLM call in a DAY-partitioned
    table with a 30-day partition expiration (TTL). Queryable via SQL.

This is the "content" half of the observability split. It holds the sensitive
payload (prompt/response text) inside the project's own GCP perimeter, while the
tracing backend (OTel → Cloud Trace / Logfire) holds only non-sensitive spans.
Rows carry ``trace_id`` so a span found in the tracer resolves to its prompt here.

Contract: ``store`` is best-effort and MUST NOT raise — callers invoke it
fire-and-forget on the hot path (``BaseAgent._call_llm``). Implementations
swallow and log their own errors; a storage failure never breaks a user request.
"""

from abc import ABC, abstractmethod

from ..domain.observability import PromptContentRecord


class PromptContentStore(ABC):
    """Store full LLM request/response content for later querying."""

    @abstractmethod
    async def store(self, record: PromptContentRecord) -> None:
        """Persist one LLM call record.

        Best-effort and non-raising: implementations must catch their own
        errors and log a warning rather than propagate, because this runs
        fire-and-forget on the request hot path.

        Args:
            record: The captured request/response and its metadata.
        """
        ...
