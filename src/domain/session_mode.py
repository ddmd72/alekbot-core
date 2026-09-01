"""
SessionMode — determines how ConversationHandler processes a message.

Resolves at the top of handle_message() based on channel binding.
All downstream logic checks mode instead of knowing about bindings.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SessionMode:
    """
    Processing mode for a conversation message.

    Default (unbound): Router flow, SessionStore history, full persistence.
    Bound, no companion_config (most bound agents today): direct delegation,
    platform API history, no persistence.
    Bound, WITH companion_config (companion-type agents, e.g. tutor): direct
    delegation, history read FROM SessionStore (history_source="session_store")
    under write_session_id, and also written there (write_session=True) under
    the same companion-shaped write_session_id — see
    ConversationHandler._resolve_session_mode.
    """
    # History source: "session_store" (Firestore) or "platform" (Slack/Telegram API)
    history_source: str = "session_store"

    # Routing: None = Router triage, "intent_name" = direct delegation
    route_intent: Optional[str] = None

    # Persistence flags
    write_session: bool = True
    write_consolidation: bool = True
    update_notification_channel: bool = True

    # Session-id to persist under when write_session=True and it differs from the
    # caller's ambient session_id (e.g. a companion channel's "platform:channel_id"
    # key, distinct from Alek's own "user_id:channel_id"). None = use the ambient
    # session_id unchanged — every existing call site is unaffected by this field.
    write_session_id: Optional[str] = None

    # Response delivery: True = thread-aware chunked, False = top-level flat
    use_threads: bool = True

    # Recent full-text turn depth for companion history tiering, resolved from
    # binding.companion_config.history_recent_full_turns. Lives here (not on the
    # companion agent's constructor) because one agent instance is a per-user
    # singleton shared across every channel that user binds it to — the depth has to
    # be resolved per message, from the CURRENT channel's binding, same as
    # write_session_id. None when unbound / no companion_config.
    history_recent_full_turns: Optional[int] = None

    @property
    def is_bound(self) -> bool:
        """Convenience: True if this is a bound channel session."""
        return self.route_intent is not None
