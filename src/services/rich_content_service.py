"""
RichContentService — application-layer dispatcher for rich content delivery.

Agents declare *what* to generate (RichContent); this service handles *how*:
  - fetches data from external sources (wttr.in, Maps, GCS, …)
  - delegates binary upload to PlatformMediaPort (platform-agnostic)

ConversationHandler calls process() for each non-table rich content item
after the text response has been delivered.

Supported types:
  weather_image  — fetches 3-day forecast PNG from wttr.in          (M1)
  file           — encodes LLM-generated Markdown → platform upload  (M2)

Planned (M3+):
  map_image      — Google Maps Static API → platform upload
"""
import aiohttp
from urllib.parse import quote

from ..ports.platform_media_port import PlatformMediaPort
from ..domain.messaging import RichContent
from ..utils.logger import logger

_WTTR_TIMEOUT = aiohttp.ClientTimeout(total=10)
_WTTR_BASE_URL = "https://wttr.in"


class RichContentService:
    """Fetches and delivers rich content items via PlatformMediaPort."""

    def __init__(self, media_port: PlatformMediaPort) -> None:
        self._media_port = media_port

    async def process(self, content: RichContent, channel_id: str) -> None:
        """
        Process a single RichContent item and deliver it to the platform.

        Args:
            content:    Structured content descriptor from LLM output
            channel_id: Platform-specific channel identifier
        """
        if content.content_type == "weather_image":
            await self._handle_weather_image(content, channel_id)
        elif content.content_type == "file":
            await self._handle_file(content, channel_id)
        else:
            logger.warning(
                "RichContentService: unsupported content type '%s' — skipping",
                content.content_type,
            )

    # ------------------------------------------------------------------
    # Private handlers
    # ------------------------------------------------------------------

    async def _handle_weather_image(
        self, content: RichContent, channel_id: str
    ) -> None:
        location = content.data.get("location", "").strip()
        if not location:
            logger.warning("RichContentService: weather_image missing 'location' in data")
            return

        try:
            image_bytes = await self._fetch_wttr_image(location)
        except Exception as e:
            logger.error(
                "RichContentService: wttr.in fetch failed for '%s' — %s", location, e
            )
            return

        await self._media_port.upload_image(
            image_bytes=image_bytes,
            alt_text=content.fallback_text or f"Weather for {location}",
            channel_id=channel_id,
        )

    async def _handle_file(self, content: RichContent, channel_id: str) -> None:
        filename = content.data.get("filename", "document.md").strip()
        title = content.data.get("title", filename).strip()
        text_content = content.data.get("content", "")

        if not text_content:
            logger.warning("RichContentService: file type missing 'content' in data")
            return

        file_bytes = text_content.encode("utf-8")
        await self._media_port.upload_file(
            file_bytes=file_bytes,
            filename=filename,
            title=title,
            channel_id=channel_id,
        )

    async def _fetch_wttr_image(self, location: str) -> bytes:
        """Fetch 3-day forecast PNG from wttr.in."""
        url = f"{_WTTR_BASE_URL}/{quote(location)}_2.png"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=_WTTR_TIMEOUT) as resp:
                resp.raise_for_status()
                return await resp.read()
