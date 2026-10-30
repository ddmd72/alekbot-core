"""Why a connect flow was started (MULTI_DRIVE_RFC §5.2, E16): each OAuth edge maps it to its realm's parameters."""
from enum import Enum


class FlowIntent(str, Enum):
    CONNECT = "connect"
    ADD = "add"
    RECONNECT = "reconnect"
