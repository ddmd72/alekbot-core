"""
SlackChannelHistorySource — fetches channel messages as LLM-ready history.

Reads the last N messages from a Slack channel via conversations.history,
converts them to domain Message objects. Used by ConversationHandler for
bound channel sessions where Slack IS the session store.
"""

from typing import List

from ...domain.llm import Message, MessagePart
from ...utils.logger import logger


class SlackChannelHistorySource:

    def __init__(self, slack_client, bot_user_id: str) -> None:
        self._client = slack_client
        self._bot_user_id = bot_user_id

    async def fetch(self, channel_id: str, limit: int = 30) -> List[Message]:
        """
        Fetch channel messages as LLM history. Oldest-first order.

        Filters out: empty messages, $commands, bot status messages.
        Role assignment: bot messages → "model", all others → "user".
        """
        try:
            resp = await self._client.conversations_history(
                channel=channel_id, limit=limit,
            )
        except Exception as e:
            logger.error(
                "❌ [ChannelHistory] Failed to fetch history for %s: %s",
                channel_id, e, exc_info=True,
            )
            return []

        messages: List[Message] = []
        for m in reversed(resp.get("messages", [])):
            text = (m.get("text") or "").strip()
            if not text or text.startswith("$"):
                continue
            # Bot's own messages → model role
            role = "model" if m.get("bot_id") or m.get("user") == self._bot_user_id else "user"
            messages.append(Message(role=role, parts=[MessagePart(text=text)]))

        return messages
