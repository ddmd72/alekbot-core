"""
Prompt ownership levels and execution-context constants.

Part of hexagonal architecture: stdlib-only domain objects.
"""

from enum import Enum

# ========================================================================
# SESSION_26: Execution Context Constants
# RFC: docs/10_rfcs/EXECUTION_CONTEXT_HEXAGONAL_RFC.md
# Purpose: Required user_id/account_id for all operations (no Optional/None)
# ========================================================================

# Anonymous context (for unregistered users - first contact before registration)
ANONYMOUS_USER_ID = "anonymous"
ANONYMOUS_ACCOUNT_ID = "guest"

# System context (for background tasks, cron jobs, internal operations)
SYSTEM_USER_ID = "system"
SYSTEM_ACCOUNT_ID = "system"


class OwnerType(Enum):
    """
    Component ownership level for 4-level priority resolution.

    Priority order: USER > ACCOUNT > AGENT > SYSTEM

    SESSION_26: Added ACCOUNT level for multi-tenant prompt customization.

    NOTE: user_id and account_id are EXECUTION CONTEXT parameters.
    Domain treats them as opaque identifiers, adapters map to DB fields.
    See: docs/10_rfcs/EXECUTION_CONTEXT_HEXAGONAL_RFC.md

    Examples:
        - SYSTEM: Default components for all agents
        - AGENT: Agent-specific overrides (quick/smart/router)
        - ACCOUNT: Account-level shared config (family/enterprise)
        - USER: User-specific customizations (highest priority)
    """
    SYSTEM = "SYSTEM"   # Default system components (lowest priority)
    AGENT = "AGENT"     # Agent-specific components
    ACCOUNT = "ACCOUNT" # Account-level shared config (NEW SESSION_26)
    USER = "USER"       # User customizations (highest priority)
