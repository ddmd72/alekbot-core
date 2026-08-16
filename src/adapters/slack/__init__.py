"""
Slack Adapters Package
HTTP Events API only — Socket Mode was removed 2026-08-16 (local-only, unused).
"""
from .base import SlackAdapter
from .http_adapter import HTTPModeAdapter

__all__ = [
    "SlackAdapter",
    "HTTPModeAdapter",
]
