"""
Billing Agent
=============

Aggregates usage reports from agents and forwards them to QuotaService.
Uses in-memory batching with periodic flush for non-blocking operation.
"""

import asyncio
import time
from collections import defaultdict
from typing import Dict, List, Any

from ..base_agent import BaseAgent
from ...domain.agent import AgentMessage, AgentResponse, AgentConfig, AgentIntent
from ...ports.quota_service import QuotaService
from ...utils.logger import logger


class BillingAgent(BaseAgent):
    """
    Infrastructure agent for usage aggregation.

    Receives INFORM messages containing usage data and batches writes
    to QuotaService to reduce write load.
    """

    def __init__(
        self,
        config: AgentConfig,
        quota_service: QuotaService,
        flush_threshold: int = 5,
        flush_interval: int = 10
    ):
        super().__init__(config)
        self.quota_service = quota_service
        self.flush_threshold = flush_threshold
        self.flush_interval = flush_interval
        self.pending_records: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self._lock = asyncio.Lock()  # Protects pending_records from concurrent access
        self._flush_task: asyncio.Task | None = None  # Started via start(), not __init__

        logger.info(
            f"💳 BillingAgent initialized (threshold={flush_threshold}, interval={flush_interval}s)"
        )

    async def start(self) -> None:
        """Start the periodic flush background task. Must be called after event loop is running."""
        self._flush_task = asyncio.create_task(self._periodic_flush())
        logger.debug("💳 BillingAgent periodic flush started")

    async def can_handle(self, message: AgentMessage) -> bool:
        if message.intent != AgentIntent.INFORM:
            return False

        payload = message.payload
        required_fields = {"account_id", "tokens", "cost", "model"}
        return required_fields.issubset(payload.keys())

    async def execute(self, message: AgentMessage) -> AgentResponse:
        payload = message.payload
        account_id = payload["account_id"]

        record = {
            "tokens": payload["tokens"],
            "cost": payload["cost"],
            "model": payload["model"],
            "agent": message.sender,
            "timestamp": time.time()
        }

        async with self._lock:
            self.pending_records[account_id].append(record)
            should_flush = len(self.pending_records[account_id]) >= self.flush_threshold

        # Flush outside lock: I/O must not block other writers
        if should_flush:
            await self._flush_account(account_id)

        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result="recorded",
            confidence=1.0
        )

    async def _flush_account(self, account_id: str) -> None:
        async with self._lock:
            records = self.pending_records.pop(account_id, [])

        # I/O outside lock
        if not records:
            return

        total_tokens = sum(r["tokens"] for r in records)
        total_cost = sum(r["cost"] for r in records)
        model = records[-1]["model"]

        logger.debug(
            f"💳 [BillingAgent] Flushing {len(records)} records "
            f"for account {account_id} (tokens={total_tokens}, cost={total_cost:.6f})"
        )

        await self.quota_service.record_usage(
            account_id=account_id,
            model=model,
            tokens=total_tokens,
            cost=total_cost
        )

    async def _periodic_flush(self) -> None:
        while True:
            await asyncio.sleep(self.flush_interval)
            async with self._lock:
                account_ids = list(self.pending_records.keys())
            for account_id in account_ids:
                await self._flush_account(account_id)

    async def shutdown(self) -> None:
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass

        async with self._lock:
            account_ids = list(self.pending_records.keys())
        for account_id in account_ids:
            await self._flush_account(account_id)
