"""
SlackMediaAdapter — uploads images and files to Slack via files_upload_v2.

Implements PlatformMediaPort for the Slack platform.
Uses the new (non-deprecated) files_upload_v2 API.
"""
from ...ports.platform_media_port import PlatformMediaPort
from ...utils.logger import logger


class SlackMediaAdapter(PlatformMediaPort):
    """Delivers binary media to a Slack channel via files_upload_v2."""

    def __init__(self, app_client, bot_token: str) -> None:
        self._client = app_client
        self._bot_token = bot_token

    async def upload_image(
        self,
        image_bytes: bytes,
        alt_text: str,
        channel_id: str,
    ) -> None:
        try:
            await self._client.files_upload_v2(
                channel=channel_id,
                file=image_bytes,
                filename="image.png",
                title=alt_text,
            )
        except Exception as e:
            logger.error("SlackMediaAdapter: upload_image failed — %s", e)
            raise

    async def upload_file(
        self,
        file_bytes: bytes,
        filename: str,
        title: str,
        channel_id: str,
    ) -> None:
        try:
            await self._client.files_upload_v2(
                channel=channel_id,
                file=file_bytes,
                filename=filename,
                title=title,
            )
        except Exception as e:
            logger.error("SlackMediaAdapter: upload_file failed — %s", e)
            raise
