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

    # Messages starting with these prefixes are topic reset markers.
    # History fetch stops when a marker is encountered (everything above is ignored).
    _TOPIC_MARKERS = frozenset({"$new", "$reset"})

    def __init__(self, slack_client, bot_user_id: str) -> None:
        self._client = slack_client
        self._bot_user_id = bot_user_id

    async def fetch(
        self, channel_id: str, limit: int = 30, exclude_last: bool = True,
    ) -> List[Message]:
        """
        Fetch channel messages as LLM history. Oldest-first order.

        Filters out: empty messages, $commands, status messages.
        Stops at topic markers ($new, $reset) — only messages AFTER the marker
        are included in history.
        Role assignment: bot messages → "model", all others → "user".

        exclude_last: if True, drops the most recent message (the current user input
        that triggered this call — the agent already receives it as the query).
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

        raw = resp.get("messages", [])
        # Drop the most recent message — it's the current user input
        if exclude_last and raw:
            raw = raw[1:]

        # raw is newest-first from Slack API.
        # Scan from newest to oldest — stop at topic marker.
        trimmed = []
        for m in raw:
            text = (m.get("text") or "").strip().lower()
            if text in self._TOPIC_MARKERS:
                break  # everything older belongs to previous topic
            trimmed.append(m)

        # Build messages oldest-first
        messages: List[Message] = []
        for m in reversed(trimmed):
            text = (m.get("text") or "").strip()
            if not text or text.startswith("$"):
                continue
            # Skip status/placeholder messages from bot
            if text in ("✅ Відповідь готова.",) or text.startswith("🤔"):
                continue
            # Bot's own messages → model role
            role = "model" if m.get("bot_id") or m.get("user") == self._bot_user_id else "user"
            messages.append(Message(role=role, parts=[MessagePart(text=text)]))

        logger.info(
            "📜 [ChannelHistory] Fetched %d messages from %s (raw=%d, after_marker=%d, limit=%d)",
            len(messages), channel_id, len(raw), len(trimmed), limit,
        )
        return messages
