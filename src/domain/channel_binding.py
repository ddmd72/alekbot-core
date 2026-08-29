"""
Channel Binding — routing override for direct agent access.

A binding maps a platform channel to a specific agent, bypassing Router.
Bound channels are stateless by default: no SessionStore writes, no consolidation.
History is fetched from the platform API (Slack conversations.history).

`companion_config` (RFC docs/10_rfcs/COMPANION_AGENTS_RFC.md §5) opts a bound
channel into session-scoped companion memory instead of the stateless default —
None means "today's default policy" (RFC §5). Not yet consumed by SessionMode
resolution; that wiring is a later phase (see the RFC's plan §9).
"""

from dataclasses import dataclass
from typing import Optional

from .companion_config import CompanionConfig


@dataclass(frozen=True)
class ChannelBinding:
    """Active binding of a platform channel to an agent."""
    channel_id: str         # Slack channel_id or Telegram chat_id
    agent_type: str         # maps to AgentDescriptor.agent_type
    intent: str             # primary intent for handle_delegation()
    created_by: str         # user_id who activated the binding
    companion_config: Optional[CompanionConfig] = None
