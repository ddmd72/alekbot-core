"""
Slack-specific implementation of ResponseChannel protocol.
Translates platform-agnostic operations into Slack API calls.
"""
import re
import random
import tempfile
import aiohttp
from typing import Any, Callable, Optional, Dict, List, Tuple
from slack_sdk.errors import SlackApiError
from ...domain.messaging import ResponseChannel, RichContent
from ...domain.ui_messages import StatusType, UIMessage
from ...domain.language import LanguageCode
from ...ports.localization_port import LocalizationPort
from ...utils.file_conversion import safe_temp_suffix
from ...utils.logger import logger


# Slack-specific constraints
SLACK_MAX_MESSAGE_LENGTH = 2500
SLACK_CHUNK_SIZE = 2000
SLACK_CHUNK_SEPARATOR = "\n\n"
# Slack rejects an empty raw_text cell (invalid_blocks: "must be more than 0 characters").
SLACK_EMPTY_CELL = "—"


class SlackResponseChannel(ResponseChannel):
    """
    Slack-specific implementation of ResponseChannel.
    
    This adapter translates generic response operations into Slack Web API calls.
    """
    
    def __init__(
        self,
        app_client,
        channel_id: str,
        bot_token: str,
        language: LanguageCode = LanguageCode.UK,
        localization: Optional[LocalizationPort] = None,
        on_long_turn: Optional[Callable[[], None]] = None,
    ):
        """
        Initialize Slack response channel.

        Args:
            app_client: Slack app client for API calls
            channel_id: Slack channel ID
            bot_token: Slack bot token for file downloads
            language: Effective UI language for this request
            localization: Localization adapter for UI phrases
            on_long_turn: Optional callback invoked once when the turn passes the
                long-turn threshold (e.g. to release a thread lock).
        """
        self.client = app_client
        self.channel_id = channel_id
        self.bot_token = bot_token
        self.platform = "slack"
        self.language = language
        self._localization = localization
        self._on_long_turn = on_long_turn
    
    @property
    def max_message_length(self) -> int:
        """Slack: 2500 chars with formatting."""
        return SLACK_MAX_MESSAGE_LENGTH
    
    @property
    def supports_message_editing(self) -> bool:
        """Slack: messages always editable."""
        return True
    
    def _resolve_links_slack(self, text: str, link_list: Optional[list]) -> str:
        """Replace numeric anchors with Slack mrkdwn links <url|title>.

        Handles two anchor styles that LLMs produce:
          [display text][N]  — Markdown reference-style → <url|display text>
          [N]                — bare numeric anchor     → <url|title from link_list>
        """
        if not link_list or not text:
            logger.debug("[SlackResponseChannel] _resolve_links_slack skipped (link_list=%r)", link_list)
            return text
        logger.debug("[SlackResponseChannel] _resolve_links_slack: %d links", len(link_list))
        index = {str(item["anchor"]): item for item in link_list if "anchor" in item}

        # Normalize "title [N]" → "[title][N]": when LLM writes the name in plain text followed
        # by a bare anchor, resolution would produce "name <url|name>" — duplicating the name.
        # Convert to reference-style so the name appears only once (as the link label).
        for anchor_str, item in index.items():
            title = item.get("title", "")
            if title:
                repl: str = f"[{title}][{anchor_str}]"
                text = re.sub(
                    rf'{re.escape(title)}\s*\[{anchor_str}\]',
                    repl.replace("\\", "\\\\"),  # literal replacement: no group/escape processing
                    text
                )

        def _replace_ref(match: re.Match) -> str:
            """Handle [display text][N] — use display text as link label."""
            display, anchor = match.group(1), match.group(2)
            item = index.get(anchor)
            if item:
                return f"<{item['url']}|{display}>"
            return match.group(0)

        def _replace_bare(match: re.Match) -> str:
            """Handle bare [N] anchor — use title from link_list."""
            anchor = match.group(1)
            item = index.get(anchor)
            if item:
                return f"<{item['url']}|{item['title']}>"
            return match.group(0)

        # Reference-style first (more specific pattern must precede bare anchor)
        text = re.sub(r"\[([^\]]+)\]\[(\d+)\]", _replace_ref, text)
        return re.sub(r"\[(\d+)\]", _replace_bare, text)

    def _format_for_platform(self, text: str) -> str:
        """Convert generic Markdown to Slack mrkdwn."""
        if not text:
            return text

        # LLM occasionally double-escapes newlines inside JSON strings (\\n → literal \n).
        # Convert literal backslash-n to real newlines before processing.
        text = text.replace("\\n", "\n")

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

    async def send_message(self, text: str, thread_id: Optional[str] = None, link_list: Optional[list] = None) -> Any:
        """
        Send a text message to Slack with length limit.

        Args:
            text: Message content
            thread_id: Optional thread timestamp
            link_list: Optional [{anchor, title, url}] — [N] anchors resolved to <url|title>

        Returns:
            Slack message response object
        """
        try:
            # Resolve link anchors before platform formatting so <url|title> survives _format_for_platform
            text = self._resolve_links_slack(text, link_list)
            # Apply Slack-specific formatting and truncation
            formatted = self._format_for_platform(text)
            if len(formatted) > SLACK_MAX_MESSAGE_LENGTH:
                formatted = formatted[:SLACK_MAX_MESSAGE_LENGTH] + self._ui_string(UIMessage.RESPONSE_TRUNCATED_SUFFIX)
            
            response = await self.client.chat_postMessage(
                channel=self.channel_id,
                text=formatted,
                thread_ts=thread_id,
                mrkdwn=True
            )
            # Normalize: when initialized with a user ID (U...), Slack auto-opens the DM
            # and returns the actual channel ID (D...) in response['channel'].
            # chat.update requires the real channel ID — capture it here so subsequent
            # update_message calls work correctly in DM notification channels.
            if response.get('channel'):
                self.channel_id = response['channel']
            return response
        except Exception as e:
            logger.error(f"❌ [SlackResponseChannel] Failed to send message: {e}")
            raise
    
    async def update_message(self, message_id: str, text: str, link_list: Optional[list] = None) -> None:
        """
        Update an existing Slack message with length limit.

        Args:
            message_id: Slack message timestamp
            text: New message content
            link_list: Optional [{anchor, title, url}] — [N] anchors resolved to <url|title>
        """
        try:
            text = self._resolve_links_slack(text, link_list)
            # Apply Slack-specific formatting BEFORE truncation
            formatted = self._format_for_platform(text)

            # Apply Slack-specific truncation
            if len(formatted) > SLACK_MAX_MESSAGE_LENGTH:
                formatted = formatted[:SLACK_MAX_MESSAGE_LENGTH] + self._ui_string(UIMessage.RESPONSE_TRUNCATED_SUFFIX)
            
            await self.client.chat_update(
                channel=self.channel_id,
                ts=message_id,
                text=formatted,
                mrkdwn=True
            )
        except Exception as e:
            logger.error(f"❌ [SlackResponseChannel] Failed to update message: {e}")
            raise

    def _resolve_and_split(self, text: str, link_list: Optional[list]) -> List[str]:
        """Resolve [N] link anchors, then split into SLACK_CHUNK_SIZE-bounded chunks.

        Shared by send_chunked_message (threaded overflow) and the main-feed overflow
        path used by send_late_answer, so link resolution behaves identically either way.
        """
        text = self._resolve_links_slack(text, link_list)
        return self._split_into_chunks(text, SLACK_CHUNK_SIZE)

    async def send_chunked_message(self, text: str, message_id: str, thread_id: Optional[str] = None, link_list: Optional[list] = None) -> None:
        """
        Send a long message by updating the first message and posting the rest as thread replies.

        Args:
            text: Full message content
            message_id: Slack message timestamp
            thread_id: Optional thread timestamp
            link_list: Optional [{anchor, title, url}] — [N] anchors resolved to <url|title>
        """
        chunks = self._resolve_and_split(text, link_list)
        if not chunks:
            return

        if len(chunks) == 1:
            await self.update_message(message_id, chunks[0])
            return

        await self.update_message(message_id, self._ui_string(UIMessage.RESPONSE_READY))

        thread_ts = thread_id if thread_id else message_id
        for chunk in chunks:
            await self.send_message(chunk, thread_ts)

    async def _send_flat_chunks(self, text: str, message_id: str, link_list: Optional[list] = None) -> None:
        """Deliver text as top-level messages only — no thread, ever.

        First chunk replaces message_id (the already-posted header); overflow chunks are
        posted as new top-level messages (no thread_ts), same shape as send_flat_response.
        Used by send_late_answer: the owner decision is the late answer stays in the main
        feed, so overflow must NOT fall back to send_chunked_message's thread_ts=message_id
        threading.
        """
        chunks = self._resolve_and_split(text, link_list)
        if not chunks:
            return

        await self.update_message(message_id, chunks[0])
        for chunk in chunks[1:]:
            await self.send_message(chunk)  # no thread_id → top-level, stays in main feed

    async def send_long_text(
        self, text: str, link_list: Optional[list] = None, thread_id: Optional[str] = None
    ) -> Any:
        """Deliver arbitrary-length text, threading overflow into chunks.

        Measures the RENDERED length (after link resolution + mrkdwn formatting) —
        the same string send_message would truncate on — so a body that sits under
        the raw limit but expands past it once [N] anchors become <url|title> is
        routed to the threaded path instead of being silently truncated.
        """
        rendered = self._format_for_platform(self._resolve_links_slack(text, link_list))
        if len(rendered) <= SLACK_MAX_MESSAGE_LENGTH:
            return await self.send_message(text, thread_id=thread_id, link_list=link_list)

        # Overflow: post a placeholder, then expand into threaded chunks. The
        # placeholder send also normalises channel_id (U… → D…) for the subsequent
        # chat.update inside send_chunked_message.
        placeholder = await self.send_message("📩", thread_id)
        await self.send_chunked_message(
            text, placeholder["ts"], thread_id=thread_id, link_list=link_list
        )
        return placeholder

    async def send_flat_response(self, text: str, status_message_id: str) -> None:
        """Send response as top-level messages. First chunk replaces status message."""
        formatted = self._format_for_platform(text)
        chunks = self._split_into_chunks(formatted, SLACK_CHUNK_SIZE)
        if not chunks:
            return
        await self.update_message(status_message_id, chunks[0])
        for chunk in chunks[1:]:
            await self.send_message(chunk)  # no thread_id → top-level

    async def send_rich_content(self, content: RichContent, thread_id: Optional[str] = None) -> Any:
        """
        Send structured rich content to Slack.

        Requirements:
        - Use Block Kit when supported (e.g., tables)
        - Always provide fallback text for notifications/search
        - Do NOT leak platform logic into domain
        """
        if content.content_type == "table":
            blocks = self._build_generic_table_blocks(content.data)
            # Slack rejects chat.postMessage with no_text when text is empty,
            # even when blocks are present. fallback_text is LLM-generated and
            # may be empty — use the table title as last resort.
            text = content.fallback_text or content.data.get("title") or "Table"
            try:
                return await self.client.chat_postMessage(
                    channel=self.channel_id,
                    text=text,
                    blocks=blocks,
                    thread_ts=thread_id,
                    mrkdwn=True
                )
            except SlackApiError as e:
                error = e.response.get("error", "") if e.response else ""
                if not error.startswith("invalid_blocks"):
                    raise
                # The reply is already generated and paid for — deliver the table as text.
                logger.warning("Slack rejected table blocks (%s): re-sending as plain text. %s", error, e)
                return await self.send_long_text(
                    self._render_table_as_text(content.data), thread_id=thread_id
                )

        if not content.fallback_text:
            return None
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
    
    @staticmethod
    def _normalize_table(data: Dict[str, Any]) -> Tuple[List[str], List[List[str]]]:
        """Headers and rows as equal-width lists of non-empty strings.

        Slack rejects a table whose rows differ in width or whose raw_text cell is empty.
        The width is the widest of header and rows, so no cell is ever dropped.
        """
        headers = list(data.get("headers") or [])
        rows = data.get("rows") or []

        # Accepted row formats (LLM may produce any):
        #   new: [{"cells": ["a","b"]}, ...]          — preferred
        #   old: [["a","b"], ...]                      — plain arrays
        #   flat: ["a","b","c","d"]                    — chunk by header count
        if isinstance(rows, dict):
            rows = list(rows.values())
        normalized: List[List[Any]] = []
        for row in rows:
            if isinstance(row, dict):
                normalized.append(list(row.get("cells", list(row.values()))))
            elif isinstance(row, list):
                normalized.append(list(row))
            else:
                normalized.append([row])
        col_count = len(headers)
        if normalized and col_count > 1 and all(len(r) == 1 for r in normalized):
            flat = [r[0] for r in normalized]
            normalized = [flat[i:i + col_count] for i in range(0, len(flat), col_count)]

        width = max([col_count] + [len(r) for r in normalized] + [1])

        def fill(cells: List[Any]) -> List[str]:
            texts = ["" if c is None else str(c) for c in cells]
            texts += [""] * (width - len(texts))
            return [t if t.strip() else SLACK_EMPTY_CELL for t in texts]

        return (fill(headers) if headers else []), [fill(r) for r in normalized]

    def _build_generic_table_blocks(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Render generic tabular data (headers + rows) using Slack table block."""
        title = data.get("title")
        footer = data.get("footer")
        headers, rows = self._normalize_table(data)

        blocks: List[Dict[str, Any]] = []
        if title:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{title}*"}
            })

        if not rows:
            return blocks

        table_rows = [
            [{"type": "raw_text", "text": cell} for cell in row]
            for row in ([headers] if headers else []) + rows
        ]
        blocks.append({
            "type": "table",
            "rows": table_rows,
            "column_settings": [{"align": "left"} for _ in table_rows[0]]
        })

        if footer:
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": footer}]
            })

        return blocks

    def _render_table_as_text(self, data: Dict[str, Any]) -> str:
        """Plain-text table for when Slack rejects the table block."""
        headers, rows = self._normalize_table(data)
        lines: List[str] = []
        if data.get("title"):
            lines.append(f"*{data['title']}*")
        lines += [" | ".join(row) for row in ([headers] if headers else []) + rows]
        if data.get("footer"):
            lines.append(data["footer"])
        return "\n".join(lines)

    def _get_status_phrases(self, status_type: StatusType) -> List[str]:
        if self._localization:
            return self._localization.get_status_phrases(self.language, status_type)
        from ...locales.uk import get_message as get_uk_message
        return get_uk_message(status_type)

    def _ui_string(self, message: UIMessage) -> str:
        if self._localization:
            return self._localization.get_ui_string(self.language, message)
        from ...locales.uk import UI_STRINGS
        return UI_STRINGS[message.value]

    async def send_status(self, status_type: StatusType, thread_id: Optional[str] = None) -> str:
        """
        Send a status message using localized phrases.

        Args:
            status_type: Semantic status type (THINKING, SEARCHING_MEMORY, etc.)
            thread_id: Optional thread timestamp

        Returns:
            Message timestamp (ID) for future updates
        """
        try:
            messages = self._get_status_phrases(status_type)
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
            messages = self._get_status_phrases(status_type)
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
        return random.choice(self._get_status_phrases(status_type))

    async def get_entertainment_intro(self) -> str:
        """Get a localized intro phrase for entertainment messages."""
        if self._localization:
            phrases = self._localization.get_entertainment_intros(self.language)
        else:
            from ...locales.uk import get_entertainment_intros
            phrases = get_entertainment_intros()
        return random.choice(phrases)

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
            messages = self._get_status_phrases(status_type)
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
            messages = self._get_status_phrases(status_type)
            phrase = random.choice(messages)

            # Update status message with emoji and animated dots
            dots = '.' * dots_count
            status_text = f"⏳ {phrase}{dots}"
            await self.update_message(message_id, status_text)
        except Exception as e:
            logger.debug(f"Failed to update status animation: {e}")
            # Don't raise - animation is non-critical
    
    async def send_document_link(self, url: str, label: str, thread_id: Optional[str] = None) -> None:
        """Send a named document link using Slack mrkdwn format: <url|label>."""
        await self.send_message(f"<{url}|{label}>", thread_id)

    async def send_file(
        self,
        content: bytes,
        filename: str,
        title: str,
        thread_id: Optional[str] = None,
    ) -> None:
        """Upload a binary file to the Slack channel via files_upload_v2."""
        try:
            await self.client.files_upload_v2(
                channel=self.channel_id,
                file=content,
                filename=filename,
                title=title,
                thread_ts=thread_id,
            )
        except Exception as e:
            logger.error("❌ [SlackResponseChannel] send_file failed: %s", e)
            raise

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
                        # Extract filename from URL. It is only a debugging aid — the real
                        # name travels in the attachment — but it must not overflow NAME_MAX:
                        # a browser-saved page carries the whole URL in its name (2026-07-31
                        # incident, errno 36 → attachment silently dropped).
                        filename = url.split('/')[-1]

                        # Create temporary file
                        with tempfile.NamedTemporaryFile(
                            delete=False, suffix=safe_temp_suffix(filename)
                        ) as tmp:
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

    async def message_link(self, message_id: Optional[str]) -> Optional[str]:
        """Resolve a Slack permalink for one of this channel's messages."""
        if not message_id:
            return None
        try:
            resp = await self.client.chat_getPermalink(channel=self.channel_id, message_ts=message_id)
            return resp.get("permalink")
        except Exception as e:
            logger.warning("⚠️ [SlackResponseChannel] permalink lookup failed: %s", e)
            return None

    async def send_late_answer(
        self,
        text: str,
        prefix: str,
        origin_message_id: Optional[str],
        link_list: Optional[list] = None,
        link: Optional[str] = None,
    ) -> None:
        """Post a long turn's answer to the main feed (never a thread), tied to its origin message.

        `link` is the origin's permalink when the caller already fetched it; only when it
        is None is it looked up here.
        """
        if link is None:
            link = await self.message_link(origin_message_id)
        header = f"{prefix} <{link}|↩>" if link else prefix
        posted = await self.client.chat_postMessage(channel=self.channel_id, text=header)
        await self._send_flat_chunks(
            f"{header}\n\n{text}", posted["ts"], link_list=link_list,
        )

    async def on_long_turn(self) -> None:
        """Fire the long-turn hook (once) — e.g. release a held thread lock."""
        hook, self._on_long_turn = self._on_long_turn, None
        if hook is not None:
            hook()
