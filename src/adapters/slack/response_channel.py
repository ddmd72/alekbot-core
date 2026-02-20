"""
Slack-specific implementation of ResponseChannel protocol.
Translates platform-agnostic operations into Slack API calls.
"""
import random
import tempfile
import aiohttp
from typing import Any, Optional, Dict, List
from ...domain.messaging import ResponseChannel, RichContent
from ...domain.ui_messages import StatusType
from ...locales.uk import get_message as get_uk_message, get_entertainment_intros
from ...utils.logger import logger
from ...utils.weather_locales import CONDITION_LABELS_UK, DAY_NAMES_UK


# Slack-specific constraints
SLACK_MAX_MESSAGE_LENGTH = 2500
SLACK_CHUNK_SIZE = 2000
SLACK_CHUNK_SEPARATOR = "\n\n"


class SlackResponseChannel(ResponseChannel):
    """
    Slack-specific implementation of ResponseChannel.
    
    This adapter translates generic response operations into Slack Web API calls.
    """
    
    def __init__(self, app_client, channel_id: str, bot_token: str):
        """
        Initialize Slack response channel.
        
        Args:
            app_client: Slack app client for API calls
            channel_id: Slack channel ID
            bot_token: Slack bot token for file downloads
        """
        self.client = app_client
        self.channel_id = channel_id
        self.bot_token = bot_token
    
    @property
    def max_message_length(self) -> int:
        """Slack: 2500 chars with formatting."""
        return SLACK_MAX_MESSAGE_LENGTH
    
    @property
    def supports_message_editing(self) -> bool:
        """Slack: messages always editable."""
        return True
    
    def _format_for_platform(self, text: str) -> str:
        """Convert generic Markdown to Slack mrkdwn."""
        if not text:
            return text

        lines = []
        for line in text.splitlines():
            stripped = line.strip()
            # Headers: # Title → *Title*
            if stripped.startswith("#"):
                stripped = stripped.lstrip("#").strip()
                if stripped:
                    line = f"*{stripped}*"
                else:
                    line = ""
            # Bullets: - item → • item
            if stripped.startswith("- "):
                line = f"• {stripped[2:].strip()}"
            lines.append(line)

        formatted = "\n".join(lines)
        # Bold and italic
        formatted = formatted.replace("**", "*")
        formatted = formatted.replace("__", "_")
        return formatted

    async def send_message(self, text: str, thread_id: Optional[str] = None) -> Any:
        """
        Send a text message to Slack with length limit.
        
        Args:
            text: Message content
            thread_id: Optional thread timestamp
            
        Returns:
            Slack message response object
        """
        try:
            # Apply Slack-specific formatting and truncation
            formatted = self._format_for_platform(text)
            if len(formatted) > SLACK_MAX_MESSAGE_LENGTH:
                formatted = formatted[:SLACK_MAX_MESSAGE_LENGTH] + "\n\n... (занадто довга відповідь)"
            
            response = await self.client.chat_postMessage(
                channel=self.channel_id,
                text=formatted,
                thread_ts=thread_id,
                mrkdwn=True
            )
            return response
        except Exception as e:
            logger.error(f"❌ [SlackResponseChannel] Failed to send message: {e}")
            raise
    
    async def update_message(self, message_id: str, text: str) -> None:
        """
        Update an existing Slack message with length limit.
        
        Args:
            message_id: Slack message timestamp
            text: New message content
        """
        try:
            # Apply Slack-specific formatting BEFORE truncation
            formatted = self._format_for_platform(text)
            
            # Apply Slack-specific truncation
            if len(formatted) > SLACK_MAX_MESSAGE_LENGTH:
                formatted = formatted[:SLACK_MAX_MESSAGE_LENGTH] + "\n\n... (занадто довга відповідь)"
            
            await self.client.chat_update(
                channel=self.channel_id,
                ts=message_id,
                text=formatted,
                mrkdwn=True
            )
        except Exception as e:
            logger.error(f"❌ [SlackResponseChannel] Failed to update message: {e}")
            raise

    async def send_chunked_message(self, text: str, message_id: str, thread_id: Optional[str] = None) -> None:
        """
        Send a long message by updating the first message and posting the rest as thread replies.

        Args:
            text: Full message content
            message_id: Slack message timestamp
            thread_id: Optional thread timestamp
        """
        chunks = self._split_into_chunks(text, SLACK_CHUNK_SIZE)
        if not chunks:
            return

        if len(chunks) == 1:
            await self.update_message(message_id, chunks[0])
            return

        await self.update_message(message_id, "✅ Відповідь готова.")

        thread_ts = thread_id if thread_id else message_id
        for chunk in chunks:
            await self.send_message(chunk, thread_ts)

    async def send_rich_content(self, content: RichContent, thread_id: Optional[str] = None) -> Any:
        """
        Send structured rich content to Slack.

        Requirements:
        - Use Block Kit when supported (e.g., tables)
        - Always provide fallback text for notifications/search
        - Do NOT leak platform logic into domain
        """
        if content.content_type == "table":
            blocks = self._build_weather_cards(content.data)
            return await self.client.chat_postMessage(
                channel=self.channel_id,
                text=content.fallback_text,
                blocks=blocks,
                thread_ts=thread_id,
                mrkdwn=True
            )

        return await self.send_message(content.fallback_text, thread_id)

    def _split_into_chunks(self, text: str, max_length: int) -> List[str]:
        """Split text into chunks with basic paragraph-aware logic."""
        if len(text) <= max_length:
            return [text]

        chunks: List[str] = []
        remaining = text
        while len(remaining) > max_length:
            split_index = remaining.rfind("\n\n", 0, max_length)
            if split_index == -1:
                split_index = remaining.rfind("\n", 0, max_length)
            if split_index == -1:
                split_index = remaining.rfind(". ", 0, max_length)
            if split_index == -1:
                split_index = max_length

            chunk = remaining[:split_index].rstrip()
            if chunk:
                chunks.append(chunk)
            remaining = remaining[split_index:].lstrip()

        if remaining:
            chunks.append(remaining)

        return chunks
    
    def _build_weather_cards(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Render weather data with cards (single day) or table (multi-day)."""
        title = data.get("title")
        rows = data.get("rows", [])
        footer = data.get("footer")

        if len(rows) > 1:
            return self._build_weather_table_blocks(title, rows, footer)

        emoji_map = {
            "sunny": "☀️",
            "cloudy": "☁️",
            "rainy": "🌧️",
            "snowy": "❄️",
            "storm": "⛈️",
            "fog": "🌫️",
            "unknown": "🌤️"
        }

        blocks: List[Dict[str, Any]] = []
        if title:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{title}*"}
            })

        for row in rows:
            if isinstance(row, list):
                cells = row
                condition_key = cells[3].lower() if len(cells) > 3 and isinstance(cells[3], str) else "unknown"
            else:
                cells = row.get("cells", [])
                condition_key = row.get("metadata", {}).get("condition", "unknown")
            emoji = emoji_map.get(condition_key, "🌤️")
            condition_label = CONDITION_LABELS_UK.get(condition_key, "Мінливо")

            day_raw = cells[0] if len(cells) > 0 else "—"
            day_label = DAY_NAMES_UK.get(day_raw, day_raw)
            night = cells[1] if len(cells) > 1 else "—"
            daytime = cells[2] if len(cells) > 2 else "—"
            humidity = cells[4] if len(cells) > 4 else "—"
            wind = cells[5] if len(cells) > 5 else "—"

            blocks.append({"type": "divider"})
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*📅 {day_label}* {emoji} {condition_label}"
                }
            })

            blocks.append({
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*День:*\n{daytime}"},
                    {"type": "mrkdwn", "text": f"*Ніч:*\n{night}"},
                    {"type": "mrkdwn", "text": f"*Вологість:*\n{humidity}"},
                    {"type": "mrkdwn", "text": f"*Вітер:*\n{wind}"}
                ]
            })

        if footer:
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": footer}]
            })

        return blocks

    def _build_weather_table_blocks(self, title: Optional[str], rows: List[Dict[str, Any]], footer: Optional[str]) -> List[Dict[str, Any]]:
        """Render multi-day forecast using Slack table block."""
        emoji_map = {
            "sunny": "☀️",
            "cloudy": "☁️",
            "rainy": "🌧️",
            "snowy": "❄️",
            "storm": "⛈️",
            "fog": "🌫️",
            "unknown": "🌤️"
        }

        blocks: List[Dict[str, Any]] = []
        if title:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{title}*"}
            })

        header_row = [
            {"type": "raw_text", "text": "День"},
            {"type": "raw_text", "text": "Ніч"},
            {"type": "raw_text", "text": "День"},
            {"type": "raw_text", "text": "Кондиція"},
            {"type": "raw_text", "text": "Вологість"},
            {"type": "raw_text", "text": "Вітер"}
        ]

        table_rows = [header_row]
        for row in rows:
            if isinstance(row, list):
                cells = row
                condition_key = cells[3].lower() if len(cells) > 3 and isinstance(cells[3], str) else "unknown"
            else:
                cells = row.get("cells", [])
                condition_key = row.get("metadata", {}).get("condition", "unknown")
            emoji = emoji_map.get(condition_key, "🌤️")
            condition_label = CONDITION_LABELS_UK.get(condition_key, "Мінливо")

            day_raw = cells[0] if len(cells) > 0 else "—"
            day_label = DAY_NAMES_UK.get(day_raw, day_raw)
            night = cells[1] if len(cells) > 1 else "—"
            daytime = cells[2] if len(cells) > 2 else "—"
            humidity = cells[4] if len(cells) > 4 else "—"
            wind = cells[5] if len(cells) > 5 else "—"

            table_rows.append([
                {"type": "raw_text", "text": f"{day_label} {emoji} {condition_label}"},
                {"type": "raw_text", "text": night},
                {"type": "raw_text", "text": daytime},
                {"type": "raw_text", "text": condition_label},
                {"type": "raw_text", "text": humidity},
                {"type": "raw_text", "text": wind}
            ])

        blocks.append({
            "type": "table",
            "block_id": "weather_table",
            "rows": table_rows,
            "column_settings": [
                {"align": "left", "is_wrapped": True},
                {"align": "center"},
                {"align": "center"},
                {"align": "left", "is_wrapped": True},
                {"align": "center"},
                {"align": "center"}
            ]
        })

        if footer:
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": footer}]
            })

        return blocks

    async def send_status(self, status_type: StatusType, thread_id: Optional[str] = None) -> str:
        """
        Send a status message using Ukrainian localization.
        
        Args:
            status_type: Semantic status type (THINKING, SEARCHING_MEMORY, etc.)
            thread_id: Optional thread timestamp
            
        Returns:
            Message timestamp (ID) for future updates
        """
        try:
            # Get Ukrainian message from centralized library
            messages = get_uk_message(status_type)
            phrase = random.choice(messages)
            
            # Send status message with emoji
            status_text = f"⏳ {phrase}."
            response = await self.send_message(status_text, thread_id)
            return response['ts']
        except Exception as e:
            logger.error(f"❌ [SlackResponseChannel] Failed to send status: {e}")
            raise
    
    async def send_status_with_phrase(self, status_type: StatusType, thread_id: Optional[str] = None) -> tuple[str, str]:
        """
        Send a status message and return both message ID and the chosen phrase.
        
        Args:
            status_type: Semantic status type
            thread_id: Optional thread timestamp
            
        Returns:
            Tuple of (message_id, phrase)
        """
        try:
            # Get Ukrainian message from centralized library
            messages = get_uk_message(status_type)
            phrase = random.choice(messages)
            
            # Send status message with emoji and single dot
            status_text = f"⏳ {phrase}."
            response = await self.send_message(status_text, thread_id)
            return response['ts'], phrase
        except Exception as e:
            logger.error(f"❌ [SlackResponseChannel] Failed to send status with phrase: {e}")
            raise
    
    async def get_status_phrase(self, status_type: StatusType) -> str:
        """
        Get a localized phrase for a status type.
        
        Args:
            status_type: Semantic status type
            
        Returns:
            Localized phrase (without emoji or dots)
        """
        messages = get_uk_message(status_type)
        return random.choice(messages)

    async def get_entertainment_intro(self) -> str:
        """Get a localized intro phrase for entertainment messages."""
        return random.choice(get_entertainment_intros())

    async def send_entertainment_message(self, text: str, thread_id: Optional[str] = None) -> Any:
        """Send entertainment message with emoji prefix."""
        return await self.send_message(f"💡 {text}", thread_id)
    
    async def update_status_with_phrase_and_dots(self, message_id: str, phrase: str, dots_count: int) -> None:
        """
        Update status message with FIXED phrase and animated dots.
        
        Args:
            message_id: Slack message timestamp
            phrase: Fixed phrase (without dots) - WILL NOT CHANGE
            dots_count: Number of dots to display (1-5)
        """
        try:
            # Use the SAME phrase, only change dots count
            dots = '.' * dots_count
            status_text = f"⏳ {phrase}{dots}"
            await self.update_message(message_id, status_text)
        except Exception as e:
            logger.debug(f"Failed to update status animation: {e}")
            # Don't raise - animation is non-critical
    
    async def update_status(self, message_id: str, status_type: StatusType) -> None:
        """
        Update an existing status message with new status type.
        
        Args:
            message_id: Slack message timestamp
            status_type: New semantic status type
        """
        try:
            # Get Ukrainian message from centralized library
            messages = get_uk_message(status_type)
            phrase = random.choice(messages)
            
            # Update status message with emoji
            status_text = f"⏳ {phrase}."
            await self.update_message(message_id, status_text)
        except Exception as e:
            logger.error(f"❌ [SlackResponseChannel] Failed to update status: {e}")
            raise
    
    async def update_status_with_dots(self, message_id: str, status_type: StatusType, dots_count: int) -> None:
        """
        Update an existing status message with animated dots.
        
        Args:
            message_id: Slack message timestamp
            status_type: Semantic status type
            dots_count: Number of dots to display (1-5)
        """
        try:
            # Get Ukrainian message from centralized library
            messages = get_uk_message(status_type)
            phrase = random.choice(messages)
            
            # Update status message with emoji and animated dots
            dots = '.' * dots_count
            status_text = f"⏳ {phrase}{dots}"
            await self.update_message(message_id, status_text)
        except Exception as e:
            logger.debug(f"Failed to update status animation: {e}")
            # Don't raise - animation is non-critical
    
    async def download_file(self, url: str, mime_type: str) -> Optional[str]:
        """
        Download a file from Slack.
        
        Args:
            url: Slack file URL
            mime_type: File MIME type
            
        Returns:
            Local file path or None if download failed
        """
        try:
            # Sanitize token to prevent header injection
            clean_token = str(self.bot_token).strip().replace("\n", "").replace("\r", "")
            headers = {'Authorization': f'Bearer {clean_token}'}
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as response:
                    if response.status == 200:
                        # Extract filename from URL
                        filename = url.split('/')[-1]
                        
                        # Create temporary file
                        with tempfile.NamedTemporaryFile(delete=False, suffix=f"_{filename}") as tmp:
                            while True:
                                chunk = await response.content.read(1024)
                                if not chunk:
                                    break
                                tmp.write(chunk)
                            return tmp.name
                    else:
                        logger.error(f"❌ [SlackResponseChannel] File download failed with status {response.status}")
                        return None
        
        except Exception as e:
            logger.error(f"❌ [SlackResponseChannel] Error downloading file: {e}")
            return None
