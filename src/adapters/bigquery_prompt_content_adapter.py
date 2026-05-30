"""
BigQueryPromptContentAdapter — PromptContentStore backed by BigQuery.

Writes one row per LLM call into a DAY-partitioned table. The partition has a
30-day expiration, so old prompt content is auto-deleted by BigQuery (TTL) —
no cleanup job, no buckets to sweep. Rows are queryable with SQL and joined to
the tracing backend via ``trace_id``.

Patterns mirror GcsFileStorageAdapter:
  - lazy client init (no auth at construction time),
  - synchronous SDK calls run in an executor (never block the event loop),
  - best-effort: every failure is swallowed + logged, never raised. This adapter
    sits on the request hot path and must never break a user response.

The table is created on first use (``_ensure_table``, idempotent) so the TTL
lives in code as the single source of truth — no manual migration step.
"""

import asyncio
from datetime import datetime, timezone
from functools import partial
from typing import Any, Optional

from ..domain.observability import PromptContentRecord
from ..ports.prompt_content_store import PromptContentStore
from ..utils.logger import logger

# 30-day TTL, expressed as BigQuery partition expiration (milliseconds).
_PARTITION_EXPIRATION_MS = 30 * 24 * 60 * 60 * 1000


class BigQueryPromptContentAdapter(PromptContentStore):
    """Persist LLM request/response content to a TTL'd BigQuery table."""

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

    # -- Public API -----------------------------------------------------------

    async def store(self, record: PromptContentRecord) -> None:
        """Insert one record. Best-effort; swallows and logs all errors."""
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
