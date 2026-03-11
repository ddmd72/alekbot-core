"""
Notification domain models.
"""
from dataclasses import dataclass
from datetime import datetime


@dataclass
class NotificationChannel:
    """Last active messaging channel for a user (platform + channel_id)."""
    user_id: str
    platform: str       # "slack" | "telegram"
    channel_id: str     # Slack channel_id or str(Telegram chat_id)
    updated_at: datetime
