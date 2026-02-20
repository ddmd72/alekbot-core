"""
Logger Agent
============

Centralized logging agent for structured logs with trace correlation.
"""

import asyncio
import time
from typing import List, Dict, Any

from ..base_agent import BaseAgent
from ...domain.agent import AgentMessage, AgentResponse, AgentConfig, AgentIntent
from ...config.environment import EnvironmentConfig
from ...ports.log_sink import LogSink
from ...utils.logger import logger


class LoggerAgent(BaseAgent):
    """
    Infrastructure agent for centralized logging.

    Receives INFORM messages with log payloads and flushes them
    to Cloud Logging in production or stdout in development.
    """

    def __init__(
        self,
        config: AgentConfig,
        env_config: EnvironmentConfig,
        log_sink: LogSink | None = None,
        buffer_size: int = 20,
        flush_interval: int = 5
    ):
        super().__init__(config)
        self.env_config = env_config
        self.buffer_size = buffer_size
        self.flush_interval = flush_interval
        self.buffer: List[Dict[str, Any]] = []
        self._lock = asyncio.Lock()  # Protects buffer from concurrent access
        self._flush_task: asyncio.Task | None = None  # Started via start(), not __init__

        self.log_sink = log_sink

        logger.info(
            f"🧾 LoggerAgent initialized (buffer_size={buffer_size}, interval={flush_interval}s)"
        )

    async def start(self) -> None:
        """Start the periodic flush background task. Must be called after event loop is running."""
        self._flush_task = asyncio.create_task(self._periodic_flush())
        logger.debug("🧾 LoggerAgent periodic flush started")

    async def can_handle(self, message: AgentMessage) -> bool:
        if message.intent != AgentIntent.INFORM:
            return False

        return "log_level" in message.payload and "message" in message.payload

    async def execute(self, message: AgentMessage) -> AgentResponse:
        entry = {
            "level": message.payload["log_level"],
            "message": message.payload["message"],
            "trace_id": message.context.get("trace_id"),
            "session_id": message.context.get("session_id"),
            "user_id": message.context.get("user_id"),
            "agent": message.sender,
            "timestamp": time.time()
        }

        async with self._lock:
            self.buffer.append(entry)
            should_flush = len(self.buffer) >= self.buffer_size

        # Flush outside lock: I/O must not block other writers
        if should_flush:
            await self._flush_logs()

        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result="logged",
            confidence=1.0
        )

    async def _flush_logs(self) -> None:
        async with self._lock:
            if not self.buffer:
                return
            entries = self.buffer[:]
            self.buffer.clear()

        # I/O outside lock
        if self.log_sink:
            for entry in entries:
                self.log_sink.log(entry)
            return

        for entry in entries:
            print(f"[{entry['level']}] {entry['agent']}: {entry['message']}")

    async def _periodic_flush(self) -> None:
        while True:
            await asyncio.sleep(self.flush_interval)
            await self._flush_logs()

    async def shutdown(self) -> None:
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass

        await self._flush_logs()
