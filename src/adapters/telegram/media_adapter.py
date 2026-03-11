"""
TelegramMediaAdapter — uploads images and files to Telegram via Bot API.

Implements PlatformMediaPort for the Telegram platform.
  upload_image → bot.send_photo   (PNG/JPEG bytes → inline photo)
  upload_file  → bot.send_document (arbitrary bytes → file attachment)

channel_id maps to Telegram chat_id (int or str, both accepted by python-telegram-bot).
"""
import io
from telegram import Bot

from ...ports.platform_media_port import PlatformMediaPort
from ...utils.logger import logger


class TelegramMediaAdapter(PlatformMediaPort):
    """Delivers binary media to a Telegram chat via Bot API."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def upload_image(
        self,
        image_bytes: bytes,
        alt_text: str,
        channel_id: str,
    ) -> None:
        try:
            await self._bot.send_photo(
                chat_id=channel_id,
                photo=io.BytesIO(image_bytes),
                caption=alt_text,
            )
        except Exception as e:
            logger.error("TelegramMediaAdapter: upload_image failed — %s", e)
            raise

    async def upload_file(
        self,
        file_bytes: bytes,
        filename: str,
        title: str,
        channel_id: str,
    ) -> None:
        try:
            await self._bot.send_document(
                chat_id=channel_id,
                document=io.BytesIO(file_bytes),
                filename=filename,
                caption=title,
            )
        except Exception as e:
            logger.error("TelegramMediaAdapter: upload_file failed — %s", e)
            raise
