"""
SessionMode — determines how ConversationHandler processes a message.

Resolves at the top of handle_message() based on channel binding.
All downstream logic checks mode instead of knowing about bindings.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class SessionMode:
    """
    Processing mode for a conversation message.

    Default (unbound): Router flow, SessionStore history, full persistence.
    Bound: direct delegation, platform API history, no persistence.
    """
    # History source: "session_store" (Firestore) or "platform" (Slack/Telegram API)
    history_source: str = "session_store"

    # Routing: None = Router triage, "intent_name" = direct delegation
    route_intent: Optional[str] = None

    # Persistence flags
    write_session: bool = True
    write_consolidation: bool = True
    update_notification_channel: bool = True

    # Response delivery: True = thread-aware chunked, False = top-level flat
    use_threads: bool = True

    @property
    def is_bound(self) -> bool:
        """Convenience: True if this is a bound channel session."""
        return self.route_intent is not None
