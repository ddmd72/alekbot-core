"""
Request Context for Multi-Tenant Operations
============================================

Provides implicit request-scoped context using Python contextvars.
Used for automatic resolution of user_id/account_id without explicit parameter passing.

Architecture:
- Domain layer (no infrastructure dependency)
- Async-safe (contextvars, not thread-local)
- Set in ConversationHandler/WebHandler
- Read in Adapters (Firestore, etc.)

Related: RFC REQUEST_CONTEXT_RFC.md
"""

from contextvars import ContextVar
from typing import Optional
from dataclasses import dataclass


# ============================================================================
# Session 27: Multi-Tenant Request Context
# RFC: docs/10_rfcs/REQUEST_CONTEXT_RFC.md
# Purpose: Implicit context for search operations without explicit ID passing
# ============================================================================

# Async-safe context variables
_current_user_id: ContextVar[Optional[str]] = ContextVar('current_user_id', default=None)
_current_account_id: ContextVar[Optional[str]] = ContextVar('current_account_id', default=None)


@dataclass
class RequestContext:
    """
    Request-scoped context for all operations.

    Set at the start of a request (ConversationHandler) and automatically
    resolved in Adapters without explicit parameter passing through agents.

    Usage:
        with RequestContext(user_id="user_123", account_id="account_456"):
            # All operations inside have access to the context
            facts = await repository.search_facts(vector, limit=10)
            # repository automatically uses account_id="account_456"

    Attributes:
        user_id: User ID (required)
        account_id: Master account ID (optional, for multi-tenant)
    """
    user_id: str
    account_id: Optional[str] = None

    def __enter__(self):
        """Set context at the start of the request."""
        self._user_token = _current_user_id.set(self.user_id)
        self._account_token = _current_account_id.set(self.account_id)
        return self

    def __exit__(self, *args):
        """Clear context at the end of the request."""
        _current_user_id.reset(self._user_token)
        _current_account_id.reset(self._account_token)

    async def __aenter__(self):
        """Async context manager support."""
        return self.__enter__()

    async def __aexit__(self, *args):
        """Async context manager support."""
        return self.__exit__(*args)


def get_current_user_id() -> Optional[str]:
    """
    Get user_id from the current request context.

    Returns:
        user_id if context is set, otherwise None

    Usage:
        user_id = get_current_user_id()
        if user_id:
            # Work with user_id
    """
    return _current_user_id.get()


def get_current_account_id() -> Optional[str]:
    """
    Get account_id from the current request context.

    Returns:
        account_id if context is set, otherwise None

    Usage:
        account_id = get_current_account_id()
        if account_id:
            # Use master account for multi-tenant
    """
    return _current_account_id.get()


def get_effective_account_id() -> Optional[str]:
    """
    Get effective account ID for multi-tenant operations.

    Logic:
    - If account_id is in context → use it (priority!)
    - Otherwise fallback to user_id

    Returns:
        account_id or user_id (for legacy compatibility)

    Usage:
        account_id = get_effective_account_id()
        facts = await repo.search_facts_by_account(account_id, ...)
    """
    return get_current_account_id() or get_current_user_id()
