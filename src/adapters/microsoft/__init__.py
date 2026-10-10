"""
Microsoft Graph Adapters Package
Shared token provider and Graph-backed adapters (To Do, and the user drive).
Modules import each other inside this package only (REQ-ARCH-23).
"""
from .graph_auth import GraphReauthRequired, MicrosoftGraphTokenProvider
from .onedrive_adapter import ONEDRIVE_PROVIDER, OneDriveAdapter
from .todo_adapter import MicrosoftToDoAdapter

__all__ = [
    "ONEDRIVE_PROVIDER",
    "OneDriveAdapter",
    "GraphReauthRequired",
    "MicrosoftGraphTokenProvider",
    "MicrosoftToDoAdapter",
]
