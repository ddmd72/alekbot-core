"""
Observability domain types — structured records for the LLM content store.

PromptContentRecord is the single row written per LLM call to a queryable
backend (BigQuery). It deliberately carries the *sensitive* half of the
observability split: full request/response text plus token/cost metadata.
The *non-sensitive* half (spans, latency, token counts) goes to the tracing
backend (OTel → Cloud Trace / Logfire); the two are joined by ``trace_id``.

Pure domain: stdlib + pydantic only. No I/O, no provider coupling.
"""

import time
from typing import Optional

from pydantic import BaseModel, Field


class PromptContentRecord(BaseModel):
    """One LLM request/response captured for queryable storage.

    Written fire-and-forget from ``BaseAgent._call_llm`` after every provider
    call. ``trace_id`` / ``span_id`` link the row back to the matching span in
    the tracing backend, so a slow span found in Logfire can be resolved to its
    exact prompt text here.
    """

    # --- Linkage to the tracing backend (the BigQuery ↔ Logfire bridge) ---
    trace_id: Optional[str] = None
    span_id: Optional[str] = None

    # --- When / who ---
    # Epoch seconds. Serialized to an ISO-8601 UTC string at the storage
    # boundary so BigQuery can DAY-partition on it (TTL lives on the partition).
    timestamp: float = Field(default_factory=time.time)
    user_id: Optional[str] = None
    account_id: Optional[str] = None

    # --- Which agent / model ---
    agent_id: str = ""
    agent_type: str = ""
    model: str = ""
    provider: Optional[str] = None
    turn: int = 0

    # --- Content (the sensitive payload — never sent to the tracing backend) ---
    request_text: Optional[str] = None
    response_text: Optional[str] = None
    tool_calls: Optional[str] = None  # JSON-serialized [{name, args}, ...]

    # --- Token / cost metadata ---
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    # --- Outcome ---
    latency_ms: Optional[float] = None
    error: Optional[str] = None
