"""
Slack Adapters Package
Supports dual-mode: Socket Mode and HTTP Events API
"""
from .base import SlackAdapter
from .socket_adapter import SocketModeAdapter
from .http_adapter import HTTPModeAdapter
from .factory import SlackAdapterFactory

__all__ = [
    "SlackAdapter",
    "SocketModeAdapter", 
    "HTTPModeAdapter",
    "SlackAdapterFactory"
]
