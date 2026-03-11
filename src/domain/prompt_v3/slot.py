"""
OwnerType enum for the v4 override hierarchy.

Part of Prompt Design System v4 (RFC: docs/10_rfcs/PROMPT_BUILDER_V4_RFC.md).
"""

from enum import Enum


class OwnerType(Enum):
    """Owner type for the 3-level override hierarchy.

    Priority (highest to lowest): USER > ACCOUNT > AGENT

    AGENT is the base profile that defines which tokens are active.
    ACCOUNT and USER can only replace tokens the agent already defines
    (matched by class + category). They cannot inject tokens into classes
    the agent has not activated.

    Note: SYSTEM from v3 is merged into AGENT in v4. There is no separate
    system-level profile — the agent profile IS the system-level definition.
    """

    AGENT = "agent"      # Base configuration (merged SYSTEM+AGENT from v3)
    ACCOUNT = "account"  # Account-level overrides
    USER = "user"        # Highest priority overrides
