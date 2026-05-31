"""
BigQueryPromptContentAdapter — PromptContentStore backed by BigQuery.

Writes one row per LLM interaction into a DAY-partitioned table with a 30-day
partition expiration (TTL) — old content auto-deletes, no cleanup job, no
buckets. Rows are queryable with SQL and joined to the tracing backend via
trace_id.

This adapter owns everything storage-shaped:
  - record building (request/response + identity → PromptContentRecord),
  - trace_id/user_id lookup from the request-scoped telemetry context,
  - the background-task set for fire-and-forget hot-path writes,
  - lazy BigQuery client + sync SDK calls in an executor,
  - idempotent table creation with the TTL in code (single source of truth).

Best-effort: every failure is swallowed + logged, never raised — this sits on
the request hot path and must never break a user response.
"""

import asyncio
import json
from datetime import datetime, timezone
from functools import partial
from typing import Any, List, Optional, TYPE_CHECKING

from ..domain.observability import PromptContentRecord
from ..ports.prompt_content_store import PromptContentStore
from ..utils.logger import logger
from ..utils.telemetry import get_trace_ids, get_request_context

if TYPE_CHECKING:
    from ..domain.llm import LLMRequest, LLMResponse

# 30-day TTL, expressed as BigQuery partition expiration (milliseconds).
_PARTITION_EXPIRATION_MS = 30 * 24 * 60 * 60 * 1000


def _render_messages(messages: list) -> str:
    """Render a Message list as readable text (role: parts) for storage."""
    lines: List[str] = []
    for msg in messages:
        role = getattr(msg, "role", "?")
        chunks: List[str] = []
        for part in getattr(msg, "parts", None) or []:
            if getattr(part, "text", None):
                chunks.append(part.text)
            tc = getattr(part, "tool_call", None)
            if tc:
                chunks.append(f"[tool_call {tc.name}({tc.args})]")
            tr = getattr(part, "tool_response", None)
            if tr:
                chunks.append(f"[tool_response {tr}]")
        lines.append(f"{role}: " + " ".join(chunks))
    return "\n".join(lines)


class BigQueryPromptContentAdapter(PromptContentStore):
    """Persist LLM interactions to a TTL'd BigQuery table."""

    # Durable-write retry policy (record_dr_result). Class-level so tests can
    # override without real sleeps.
    _DURABLE_ATTEMPTS = 3
    _DURABLE_BACKOFF_S = 2.0

    def __init__(self, dataset: str, table: str, project: str = "") -> None:
        """
        Args:
            dataset: BigQuery dataset id (created on first use if missing).
            table:   Table id within the dataset.
            project: GCP project. Empty → BigQuery client's default project.
        """
        self._dataset = dataset
        self._table = table
        self._project = project
        self._client: Optional[Any] = None  # lazy
        self._table_ready = False
        self._ensure_lock = asyncio.Lock()
        self._bg_tasks: set = set()  # holds fire-and-forget writes (GC guard)

    # -- Public API -----------------------------------------------------------

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
        """Best-effort: build the record, schedule a background write, return."""
        try:
            record = self._build_record(
                request, response, agent_id, agent_type, account_id, turn, latency_ms, provider
            )
            task = asyncio.create_task(self._store(record))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        except Exception as e:  # building/scheduling must never break the LLM path
            logger.warning("BigQueryPromptContentAdapter: record_turn skipped: %s", e)

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
        """Durable: awaited + retried write for an expensive deep-research result."""
        record = self._build_dr_record(
            output_text=output_text,
            query=query,
            user_id=user_id,
            account_id=account_id,
            model=model,
            provider=provider,
            source=source,
            pass_index=pass_index,
            total_tokens=total_tokens,
        )
        await self._store_durable(record)

    @staticmethod
    def _build_dr_record(
        *,
        output_text: str,
        query: str,
        user_id: Optional[str],
        account_id: Optional[str],
        model: str,
        provider: str,
        source: str,
        pass_index: Optional[int],
        total_tokens: int,
    ) -> PromptContentRecord:
        # pass_index is tagged via the `turn` column (no schema migration): rows
        # are already disambiguated by agent_type="deep_research"; within those,
        # turn = pass (1 = first/round-1, 2 = critic/final, 0 = single-pass).
        ids = get_trace_ids()
        return PromptContentRecord(
            trace_id=ids.get("trace_id"),
            span_id=ids.get("span_id"),
            user_id=user_id or None,
            account_id=account_id or None,
            agent_id=source,
            agent_type="deep_research",
            model=model or "",
            provider=provider or None,
            turn=pass_index or 0,
            request_text=query or None,
            response_text=output_text,
            total_tokens=total_tokens,
            latency_ms=None,
        )

    # -- Record building (adapter-owned; domain never sees storage shapes) -----

    @staticmethod
    def _build_record(
        request: "LLMRequest",
        response: "LLMResponse",
        agent_id: str,
        agent_type: str,
        account_id: Optional[str],
        turn: int,
        latency_ms: float,
        provider: str,
    ) -> PromptContentRecord:
        ids = get_trace_ids()
        ctx = get_request_context()
        m = response.usage_metadata
        tool_calls = (
            json.dumps(
                [{"name": tc.name, "args": tc.args} for tc in response.tool_calls],
                ensure_ascii=False,
            )
            if response.tool_calls
            else None
        )
        return PromptContentRecord(
            trace_id=ids.get("trace_id"),
            span_id=ids.get("span_id"),
            user_id=ctx.get("user_id"),
            account_id=account_id,
            agent_id=agent_id,
            agent_type=agent_type,
            model=request.model_name or "",
            provider=provider or None,
            turn=turn,
            request_text=BigQueryPromptContentAdapter._serialize_request(request),
            response_text=response.text,
            tool_calls=tool_calls,
            prompt_tokens=getattr(m, "prompt_tokens", 0) if m else 0,
            completion_tokens=getattr(m, "completion_tokens", 0) if m else 0,
            total_tokens=getattr(m, "total_tokens", 0) if m else 0,
            cache_read_tokens=getattr(m, "cache_read_tokens", 0) if m else 0,
            cache_creation_tokens=getattr(m, "cache_creation_tokens", 0) if m else 0,
            latency_ms=latency_ms,
        )

    @staticmethod
    def _serialize_request(request: "LLMRequest") -> str:
        """Render system instruction + message history as a single text blob."""
        parts: List[str] = []
        system = getattr(request, "system_instruction", None)
        if system:
            parts.append(f"=== SYSTEM ===\n{system}")
        parts.append("=== MESSAGES ===")
        parts.append(_render_messages(getattr(request, "messages", None) or []))
        return "\n\n".join(parts)

    # -- Write (best-effort, swallows) ----------------------------------------

    async def _store(self, record: PromptContentRecord) -> None:
        try:
            await self._ensure_table()
            row = self._to_row(record)
            loop = asyncio.get_event_loop()
            errors = await loop.run_in_executor(None, partial(self._insert_sync, row))
            if errors:
                logger.warning(
                    "BigQueryPromptContentAdapter: insert returned errors: %s", errors
                )
        except Exception as e:  # never propagate into the request path
            logger.warning("BigQueryPromptContentAdapter: store failed: %s", e)

    async def _store_durable(self, record: PromptContentRecord) -> None:
        """Awaited write with retry — for expensive content that must not be lost.

        Still non-raising: on exhausted retries it logs an error and returns, so a
        BigQuery outage never blocks delivering the (already-computed) result to
        the user. Callers invoke this BEFORE delivery so the content is persisted
        even if delivery later fails.
        """
        last: Any = None
        for attempt in range(self._DURABLE_ATTEMPTS):
            try:
                await self._ensure_table()
                row = self._to_row(record)
                loop = asyncio.get_event_loop()
                errors = await loop.run_in_executor(None, partial(self._insert_sync, row))
                if not errors:
                    return
                last = errors
            except Exception as e:
                last = e
            if attempt < self._DURABLE_ATTEMPTS - 1:
                await asyncio.sleep(self._DURABLE_BACKOFF_S * (attempt + 1))
        logger.error(
            "BigQueryPromptContentAdapter: durable store failed after %d attempts: %s",
            self._DURABLE_ATTEMPTS, last,
        )

    # -- Lazy client / table --------------------------------------------------

    def _get_client(self):
        """Lazy-initialize the BigQuery client on first use."""
        if self._client is None:
            from google.cloud import bigquery

            self._client = bigquery.Client(project=self._project or None)
        return self._client

    def _table_id(self) -> str:
        client = self._get_client()
        project = self._project or client.project
        return f"{project}.{self._dataset}.{self._table}"

    async def _ensure_table(self) -> None:
        """Create the dataset + DAY-partitioned table (30-day TTL) if missing."""
        if self._table_ready:
            return
        async with self._ensure_lock:
            if self._table_ready:
                return
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._ensure_table_sync)
            self._table_ready = True

    # -- Synchronous SDK operations (run in executor only) --------------------

    def _ensure_table_sync(self) -> None:
        from google.cloud import bigquery

        client = self._get_client()
        client.create_dataset(self._dataset, exists_ok=True)

        schema = [
            bigquery.SchemaField("trace_id", "STRING"),
            bigquery.SchemaField("span_id", "STRING"),
            bigquery.SchemaField("timestamp", "TIMESTAMP"),
            bigquery.SchemaField("user_id", "STRING"),
            bigquery.SchemaField("account_id", "STRING"),
            bigquery.SchemaField("agent_id", "STRING"),
            bigquery.SchemaField("agent_type", "STRING"),
            bigquery.SchemaField("model", "STRING"),
            bigquery.SchemaField("provider", "STRING"),
            bigquery.SchemaField("turn", "INTEGER"),
            bigquery.SchemaField("request_text", "STRING"),
            bigquery.SchemaField("response_text", "STRING"),
            bigquery.SchemaField("tool_calls", "STRING"),
            bigquery.SchemaField("prompt_tokens", "INTEGER"),
            bigquery.SchemaField("completion_tokens", "INTEGER"),
            bigquery.SchemaField("total_tokens", "INTEGER"),
            bigquery.SchemaField("cache_read_tokens", "INTEGER"),
            bigquery.SchemaField("cache_creation_tokens", "INTEGER"),
            bigquery.SchemaField("latency_ms", "FLOAT"),
            bigquery.SchemaField("error", "STRING"),
        ]
        table = bigquery.Table(self._table_id(), schema=schema)
        table.time_partitioning = bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY,
            field="timestamp",
            expiration_ms=_PARTITION_EXPIRATION_MS,
        )
        client.create_table(table, exists_ok=True)

    def _insert_sync(self, row: dict) -> list:
        client = self._get_client()
        return client.insert_rows_json(self._table_id(), [row])

    # -- Serialization --------------------------------------------------------

    @staticmethod
    def _to_row(record: PromptContentRecord) -> dict:
        """Map the domain record to a BigQuery row (timestamp → ISO-8601 UTC)."""
        data = record.model_dump()
        data["timestamp"] = datetime.fromtimestamp(
            record.timestamp, tz=timezone.utc
        ).isoformat()
        return data
