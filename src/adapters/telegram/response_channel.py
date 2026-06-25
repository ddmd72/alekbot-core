"""
Telegram-specific implementation of ResponseChannel protocol.
"""
import re
import random
import tempfile
import aiohttp
from typing import Any, Optional, List
from telegram import Bot
from ...domain.messaging import ResponseChannel, RichContent
from ...domain.ui_messages import StatusType, UIMessage
from ...domain.language import LanguageCode
from ...ports.localization_port import LocalizationPort
from ...utils.message_chunker import MessageChunker
from ...utils.logger import logger


# Telegram-specific constraints
TELEGRAM_MAX_MESSAGE_LENGTH = 4096
TELEGRAM_CHUNK_SIZE = 4000


class TelegramResponseChannel(ResponseChannel):
    """
    Telegram-specific implementation of ResponseChannel.
    
    Uses plain text (not MarkdownV2) for MVP to avoid escaping complexity.
    """

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        language: LanguageCode = LanguageCode.UK,
        localization: Optional[LocalizationPort] = None,
    ):
        """
        Initialize Telegram response channel.

        Args:
            bot: Telegram Bot instance (python-telegram-bot)
            chat_id: Telegram chat ID
            language: Effective UI language for this request
            localization: Localization adapter for UI phrases
        """
        self.bot = bot
        self.chat_id = chat_id
        self.channel_id = str(chat_id)
        self.platform = "telegram"
        self.language = language
        self._localization = localization
        self.chunker = MessageChunker(max_length=TELEGRAM_CHUNK_SIZE)
        logger.info(f"✅ TelegramResponseChannel initialized for chat {chat_id}")

    @property
    def max_message_length(self) -> int:
        """Telegram: 4096 chars."""
        return TELEGRAM_MAX_MESSAGE_LENGTH

    @property
    def supports_message_editing(self) -> bool:
        """Telegram: messages editable for 48h."""
        return True

    def _format_for_platform(self, text: str) -> str:
        """
        Format text for Telegram MarkdownV2.
        
        Converts generic Markdown to Telegram's MarkdownV2 format:
        - Escapes special characters
        - Converts ** to * (bold)
        - Preserves inline code with `
        
        https://core.telegram.org/bots/api#markdownv2-style
        """
        if not text:
            return text
        
        # Special characters that need escaping in MarkdownV2
        # BUT we preserve *, _, `, [ for markdown formatting
        chars_to_escape = ['_', '[', ']', '(', ')', '~', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
        
        # Step 1: Temporarily protect markdown syntax
        # Replace ** with placeholder before escaping
        text = text.replace('**', '⚡BOLD⚡')
        text = text.replace('__', '⚡ITALIC⚡')
        
        # Step 2: Escape special chars (but not *, _, `)
        for char in chars_to_escape:
            text = text.replace(char, f'\\{char}')
        
        # Step 3: Restore markdown syntax
        text = text.replace('⚡BOLD⚡', '*')  # Bold: ** → *
        text = text.replace('⚡ITALIC⚡', '_')  # Italic: __ → _
        
        return text

    _TG_ESCAPE_CHARS = ['_', '[', ']', '(', ')', '~', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']

    def _escape_tg(self, text: str) -> str:
        """Escape special chars for Telegram MarkdownV2 plain text."""
        for char in self._TG_ESCAPE_CHARS:
            text = text.replace(char, f'\\{char}')
        return text

    def _resolve_links_telegram(self, formatted_text: str, link_list: Optional[List]) -> str:
        """
        Replace escaped anchors with Telegram MarkdownV2 links [title](url).

        Must be called AFTER _format_for_platform because _format_for_platform escapes
        '[' → '\\[' and ']' → '\\]', so after formatting:
          [display text][N]  becomes  \\[display text\\]\\[N\\]
          [N]                becomes  \\[N\\]

        Handles both patterns:
          \\[display text\\]\\[N\\]  → [display text](url)  (reference-style)
          \\[N\\]                    → [escaped title](url)  (bare anchor)

        In the URL part only ')' and '\\' need escaping per Telegram MarkdownV2 spec.
        """
        if not link_list or not formatted_text:
            return formatted_text
        index = {str(item["anchor"]): item for item in link_list if "anchor" in item}

        # Normalize "escaped_title \[N\]" → "\[escaped_title\]\[N\]".
        # _format_for_platform already escaped [ → \[ and ] → \], so bare anchor \[N\] follows
        # the already-escaped title. Convert to reference-style to avoid name duplication.
        for anchor_str, item in index.items():
            title = item.get("title", "")
            if title:
                escaped_title = self._escape_tg(title)
                repl = f"\\[{escaped_title}\\]\\[{anchor_str}\\]"
                formatted_text = re.sub(
                    rf'{re.escape(escaped_title)}\s*\\\[{anchor_str}\\\]',
                    lambda m, r=repl: r,
                    formatted_text
                )

        def _url(raw: str) -> str:
            return raw.replace("\\", "\\\\").replace(")", "\\)")

        def _replace_ref(match: re.Match) -> str:
            """Handle \\[display text\\]\\[N\\] — display text already escaped."""
            display, anchor = match.group(1), match.group(2)
            item = index.get(anchor)
            if not item:
                return match.group(0)
            return f"[{display}]({_url(item['url'])})"

        def _replace_bare(match: re.Match) -> str:
            """Handle bare \\[N\\] — use title from link_list, escape it."""
            anchor = match.group(1)
            item = index.get(anchor)
            if not item:
                return match.group(0)
            title = self._escape_tg(item["title"])
            return f"[{title}]({_url(item['url'])})"

        # Reference-style first (more specific), then bare anchors
        text = re.sub(r"\\\[(.*?)\\\]\\\[(\d+)\\\]", _replace_ref, formatted_text)
        return re.sub(r"\\\[(\d+)\\\]", _replace_bare, text)

    def _validate_markdown_pairs(self, text: str) -> bool:
        """
        Validate that markdown formatting tags are properly paired.
        
        Telegram MarkdownV2 requires paired * and _ for bold/italic.
        Unpaired tags cause "can't parse entities" errors.
        
        Args:
            text: Formatted text with MarkdownV2 syntax
            
        Returns:
            True if all tags are properly paired
        """
        # Count asterisks (bold)
        asterisk_count = text.count('*')
        # Count underscores (italic) - but exclude escaped ones
        underscore_count = text.count('_') - text.count('\\_')
        
        return asterisk_count % 2 == 0 and underscore_count % 2 == 0

    def _sanitize_unpaired_tags(self, text: str) -> str:
        """
        Remove unpaired markdown tags to prevent Telegram parsing errors.
        
        This is a safety fallback when truncation breaks markdown syntax.
        
        Args:
            text: Formatted text that may have unpaired tags
            
        Returns:
            Text with unpaired tags removed
        """
        # Remove unpaired asterisks (bold)
        asterisk_count = text.count('*')
        if asterisk_count % 2 != 0:
            # Remove last unpaired asterisk
            last_idx = text.rfind('*')
            if last_idx != -1:
                text = text[:last_idx] + text[last_idx+1:]
                logger.debug("🔧 [Telegram] Removed unpaired asterisk at position %d", last_idx)
        
        # Remove unpaired underscores (italic) - but preserve escaped ones
        underscore_count = text.count('_') - text.count('\\_')
        if underscore_count % 2 != 0:
            # Find last non-escaped underscore
            for i in range(len(text) - 1, -1, -1):
                if text[i] == '_' and (i == 0 or text[i-1] != '\\'):
                    text = text[:i] + text[i+1:]
                    logger.debug("🔧 [Telegram] Removed unpaired underscore at position %d", i)
                    break
        
        return text

    async def send_message(self, text: str, thread_id: Optional[str] = None, link_list: Optional[List] = None) -> Any:
        """
        Send a text message to Telegram with graceful markdown fallback.

        Strategy:
        1. Try with MarkdownV2 formatting
        2. If parsing fails due to unpaired tags, sanitize and retry
        3. If still fails, fallback to plain text

        Args:
            text: Message content
            thread_id: Optional message_thread_id for topics
            link_list: Optional [{anchor, title, url}] — [N] anchors resolved to [title](url)

        Returns:
            Message ID (as string)
        """
        try:
            # Truncate BEFORE formatting to account for escaping overhead
            # MarkdownV2 escaping can add ~30% extra characters
            safe_length = int(TELEGRAM_MAX_MESSAGE_LENGTH * 0.7)  # 2867 chars safety margin

            if len(text) > safe_length:
                text = text[:safe_length] + self._ui_string(UIMessage.RESPONSE_TRUNCATED_SUFFIX)

            formatted = self._format_for_platform(text)
            # Resolve link anchors AFTER formatting: \[N\] → [title](url)
            formatted = self._resolve_links_telegram(formatted, link_list)

            # Validate markdown pairs
            if not self._validate_markdown_pairs(formatted):
                logger.warning("⚠️ [Telegram] Unpaired markdown tags detected, sanitizing...")
                formatted = self._sanitize_unpaired_tags(formatted)

            # Final safety check after formatting
            if len(formatted) > TELEGRAM_MAX_MESSAGE_LENGTH:
                # Fallback: aggressive truncation if escaping blew up the length
                formatted = formatted[:TELEGRAM_MAX_MESSAGE_LENGTH - 50] + "\n\n..."

            message = await self.bot.send_message(
                chat_id=self.chat_id,
                text=formatted,
                parse_mode="MarkdownV2",
                message_thread_id=int(thread_id) if thread_id else None
            )
            return str(message.message_id)

        except Exception as e:
            error_msg = str(e)
            # Telegram API returns "can't parse entities" for markdown errors
            if "can't parse entities" in error_msg or "can't find end of" in error_msg:
                logger.warning(
                    f"⚠️ [Telegram] MarkdownV2 parsing failed: {error_msg}. "
                    f"Falling back to plain text."
                )
                try:
                    # Fallback: send as plain text without formatting
                    message = await self.bot.send_message(
                        chat_id=self.chat_id,
                        text=text,  # Original text, NO formatting
                        parse_mode=None,
                        message_thread_id=int(thread_id) if thread_id else None
                    )
                    logger.info("✅ [Telegram] Plain text fallback successful")
                    return str(message.message_id)
                except Exception as fallback_error:
                    logger.error(f"❌ [Telegram] Plain text fallback also failed: {fallback_error}")
                    raise
            
            logger.error(f"❌ [TelegramResponseChannel] Failed to send message: {e}")
            raise

    async def update_message(self, message_id: str, text: str, link_list: Optional[List] = None) -> None:
        """
        Update an existing Telegram message with graceful markdown fallback.

        Strategy:
        1. Try with MarkdownV2 formatting
        2. If parsing fails, sanitize and retry
        3. If still fails, fallback to plain text
        4. If message too old (>48h), send new message

        Args:
            message_id: Telegram message ID
            text: New message content
            link_list: Optional [{anchor, title, url}] — [N] anchors resolved to [title](url)
        """
        try:
            # Truncate BEFORE formatting to account for escaping overhead
            safe_length = int(TELEGRAM_MAX_MESSAGE_LENGTH * 0.7)

            if len(text) > safe_length:
                text = text[:safe_length] + "\n\n..."

            formatted = self._format_for_platform(text)
            formatted = self._resolve_links_telegram(formatted, link_list)

            # Validate markdown pairs
            if not self._validate_markdown_pairs(formatted):
                logger.warning("⚠️ [Telegram] Unpaired markdown tags detected, sanitizing...")
                formatted = self._sanitize_unpaired_tags(formatted)

            # Final safety check
            if len(formatted) > TELEGRAM_MAX_MESSAGE_LENGTH:
                formatted = formatted[:TELEGRAM_MAX_MESSAGE_LENGTH - 50] + "\n\n..."

            await self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=int(message_id),
                text=formatted,
                parse_mode="MarkdownV2"
            )

        except Exception as e:
            error_msg = str(e)
            
            # Handle markdown parsing errors
            if "can't parse entities" in error_msg or "can't find end of" in error_msg:
                logger.warning(
                    f"⚠️ [Telegram] MarkdownV2 parsing failed on update: {error_msg}. "
                    f"Falling back to plain text."
                )
                try:
                    # Fallback: update with plain text
                    await self.bot.edit_message_text(
                        chat_id=self.chat_id,
                        message_id=int(message_id),
                        text=text,  # Original text, NO formatting
                        parse_mode=None
                    )
                    logger.info("✅ [Telegram] Plain text update successful")
                    return
                except Exception as fallback_error:
                    logger.error(f"❌ [Telegram] Plain text update also failed: {fallback_error}")
                    # Continue to final fallback (new message)
            
            # Telegram: messages older than 48h cannot be edited
            logger.warning(f"⚠️ [TelegramResponseChannel] Cannot edit message {message_id}: {e}")
            # Final fallback: send new message
            await self.send_message(text, thread_id=None)

    async def send_chunked_message(
        self,
        text: str,
        message_id: str,
        thread_id: Optional[str] = None,
        link_list: Optional[List] = None,
    ) -> None:
        """
        Send long message as multiple messages.

        Args:
            text: Full message content
            message_id: Message to update with first chunk
            thread_id: Optional thread identifier
            link_list: Optional [{anchor, title, url}] — [N] anchors resolved to [title](url)
        """
        chunks = self.chunker.split(text)

        if len(chunks) == 1:
            await self.update_message(message_id, chunks[0], link_list=link_list)
            return

        # Update first message (no content — link_list not needed here)
        await self.update_message(message_id, self._ui_string(UIMessage.RESPONSE_READY))

        # Send remaining chunks; link_list applies to all chunks
        for chunk in chunks:
            await self.send_message(chunk, thread_id=thread_id or message_id, link_list=link_list)

    async def send_long_text(
        self, text: str, link_list: Optional[List] = None, thread_id: Optional[str] = None
    ) -> Any:
        """Deliver arbitrary-length text, threading overflow into chunks.

        Mirrors send_message's truncation gates so the single-vs-thread decision is
        made on the same lengths send_message would truncate on: the RAW text at
        0.7×max (MarkdownV2 escaping inflates length) and the rendered text at the
        hard max. A body that expands past either gate once [N] anchors become
        [title](url) is routed to the threaded path instead of being truncated.
        """
        safe_length = int(TELEGRAM_MAX_MESSAGE_LENGTH * 0.7)
        rendered = self._resolve_links_telegram(self._format_for_platform(text), link_list)
        if len(text) <= safe_length and len(rendered) <= TELEGRAM_MAX_MESSAGE_LENGTH:
            return await self.send_message(text, thread_id=thread_id, link_list=link_list)

        placeholder = await self.send_message("📩", thread_id)
        await self.send_chunked_message(
            text, placeholder, thread_id=thread_id, link_list=link_list
        )
        return placeholder

    async def send_flat_response(self, text: str, status_message_id: str) -> None:
        """Send response as top-level messages. First chunk replaces status message."""
        chunks = self.chunker.split(text)
        if not chunks:
            return
        await self.update_message(status_message_id, chunks[0])
        for chunk in chunks[1:]:
            await self.send_message(chunk)  # no thread_id → top-level

    async def send_rich_content(
        self,
        content: RichContent,
        thread_id: Optional[str] = None
    ) -> Any:
        """
        Send rich content to Telegram.

        For MVP: use fallback text only.
        TODO: Implement Inline Keyboards for tables.
        """
        return await self.send_message(content.fallback_text, thread_id)

    def _get_status_phrases(self, status_type: StatusType) -> list:
        if self._localization:
            return self._localization.get_status_phrases(self.language, status_type)
        from ...locales.uk import get_message as get_uk_message
        return get_uk_message(status_type)

    def _ui_string(self, message: UIMessage) -> str:
        if self._localization:
            return self._localization.get_ui_string(self.language, message)
        from ...locales.uk import UI_STRINGS
        return UI_STRINGS[message.value]

    async def send_status(
        self,
        status_type: StatusType,
        thread_id: Optional[str] = None
    ) -> str:
        """Send status message using localized phrases."""
        try:
            messages = self._get_status_phrases(status_type)
            phrase = random.choice(messages)

            status_text = f"⏳ {phrase}."
            message_id = await self.send_message(status_text, thread_id)
            return message_id

        except Exception as e:
            logger.error(f"❌ [TelegramResponseChannel] Failed to send status: {e}")
            raise

    async def send_status_with_phrase(
        self,
        status_type: StatusType,
        thread_id: Optional[str] = None
    ) -> tuple[str, str]:
        """Send status message and return (message_id, phrase)."""
        try:
            messages = self._get_status_phrases(status_type)
            phrase = random.choice(messages)

            status_text = f"⏳ {phrase}."
            message_id = await self.send_message(status_text, thread_id)
            return message_id, phrase

        except Exception as e:
            logger.error(f"❌ [TelegramResponseChannel] Failed to send status with phrase: {e}")
            raise

    async def get_status_phrase(self, status_type: StatusType) -> str:
        """Get localized phrase for status type."""
        return random.choice(self._get_status_phrases(status_type))

    async def get_entertainment_intro(self) -> str:
        """Get localized intro phrase for entertainment messages."""
        if self._localization:
            phrases = self._localization.get_entertainment_intros(self.language)
        else:
            from ...locales.uk import get_entertainment_intros
            phrases = get_entertainment_intros()
        return random.choice(phrases)

    async def send_entertainment_message(
        self,
        text: str,
        thread_id: Optional[str] = None
    ) -> Any:
        """Send entertainment message with emoji prefix."""
        return await self.send_message(f"💡 {text}", thread_id)

    async def update_status_with_phrase_and_dots(
        self,
        message_id: str,
        phrase: str,
        dots_count: int
    ) -> None:
        """Update status message with fixed phrase and animated dots."""
        try:
            dots = '.' * dots_count
            status_text = f"⏳ {phrase}{dots}"
            await self.update_message(message_id, status_text)
        except Exception:
            logger.debug("Status animation update failed (non-critical)")

    async def update_status(
        self,
        message_id: str,
        status_type: StatusType
    ) -> None:
        """Update existing status message with new status type."""
        try:
            messages = self._get_status_phrases(status_type)
            phrase = random.choice(messages)

            status_text = f"⏳ {phrase}."
            await self.update_message(message_id, status_text)

        except Exception as e:
            logger.error(f"❌ [TelegramResponseChannel] Failed to update status: {e}")
            raise

    async def update_status_with_dots(
        self,
        message_id: str,
        status_type: StatusType,
        dots_count: int
    ) -> None:
        """Update existing status message with animated dots."""
        try:
            messages = self._get_status_phrases(status_type)
            phrase = random.choice(messages)

            dots = '.' * dots_count
            status_text = f"⏳ {phrase}{dots}"
            await self.update_message(message_id, status_text)
        except Exception:
            logger.debug("Status animation update failed (non-critical)")

    async def send_document_link(self, url: str, label: str, thread_id: Optional[str] = None) -> None:
        """Send a named document link using Telegram Markdown format."""
        await self.send_message(f"[{label}]({url})", thread_id)

    async def send_file(
        self,
        content: bytes,
        filename: str,
        title: str,
        thread_id: Optional[str] = None,
    ) -> None:
        """Upload a binary file to the Telegram chat via sendDocument."""
        try:
            import io
            await self.bot.send_document(
                chat_id=self.chat_id,
                document=io.BytesIO(content),
                filename=filename,
                caption=title,
            )
        except Exception as e:
            logger.error("❌ [TelegramResponseChannel] send_file failed: %s", e)
            raise

    async def download_file(
        self,
        url: str,
        mime_type: str
    ) -> Optional[str]:
        """
        Download file from Telegram.
        
        Telegram files are accessible via public URL (no auth needed).
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        filename = url.split('/')[-1]

                        with tempfile.NamedTemporaryFile(delete=False, suffix=f"_{filename}") as tmp:
                            while True:
                                chunk = await response.content.read(1024)
                                if not chunk:
                                    break
                                tmp.write(chunk)
                            return tmp.name
                    else:
                        logger.error(f"❌ [TelegramResponseChannel] File download failed with status {response.status}")
                        return None

        except Exception as e:
            logger.error(f"❌ [TelegramResponseChannel] Error downloading file: {e}")
            return None
