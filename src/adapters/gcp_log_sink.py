"""
GCP Log Sink Adapter
====================

Concrete LogSink implementation for Google Cloud Logging.
"""
from typing import Dict, Any

from google.cloud import logging as cloud_logging

from ..ports.log_sink import LogSink


class GcpLogSink(LogSink):
    """Adapter for Google Cloud Logging."""

    def __init__(self, logger_name: str = "alek-core") -> None:
        client = cloud_logging.Client()
        self._logger = client.logger(logger_name)

    def log(self, entry: Dict[str, Any]) -> None:
        self._logger.log_struct(entry, severity=entry.get("level", "INFO"))