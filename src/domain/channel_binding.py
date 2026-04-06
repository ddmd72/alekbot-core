"""
Channel Binding — routing override for direct agent access.

A binding maps a platform channel to a specific agent, bypassing Router.
Bound channels are stateless: no SessionStore writes, no consolidation.
History is fetched from the platform API (Slack conversations.history).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ChannelBinding:
    """Active binding of a platform channel to an agent."""
    channel_id: str         # Slack channel_id or Telegram chat_id
    agent_type: str         # maps to AgentDescriptor.agent_type
    intent: str             # primary intent for handle_delegation()
    created_by: str         # user_id who activated the binding
