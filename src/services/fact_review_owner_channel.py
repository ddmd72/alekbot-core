"""FactReviewOwnerChannel — how the memory review reaches the owner: which channel a question goes to, the post there, the
digest, and the channel whose recent messages the review pass reads."""
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..ports.channel_binding_port import ChannelBindingPort
from ..ports.interactive_message_port import InteractiveMessagePort
from ..ports.notification_state_port import NotificationStatePort

if TYPE_CHECKING:
    from .user_notification_service import UserNotificationService


@dataclass(frozen=True)
class FactReviewOwnerChannel:
    """notification_state, channel_bindings: the owner's primary channel and whether a question may go there;
    interactive: the question's post on it; notifications: the digest, and the channel of the owner's recent messages."""

    notification_state: NotificationStatePort
    channel_bindings: ChannelBindingPort
    notifications: "UserNotificationService"
    interactive: InteractiveMessagePort
