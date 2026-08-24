"""
VideoGenerationPort — system boundary for AI video generation/editing services.

Implementations: GrokVideoAdapter (src/adapters/grok_video_adapter.py).
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class VideoPollResult:
    """Result of one get_status() poll."""
    status: str                    # "pending" | "done" | "failed" | "expired"
    data: Optional[bytes] = None   # populated only when status == "done"
    mime_type: str = "video/mp4"   # unconfirmed against a live response — see RFC §10 #2
    error: str = ""                # populated on "failed"


class VideoGenerationPort(ABC):
    """Generate and edit videos via an external AI video model."""

    @abstractmethod
    async def create_video(
        self, prompt: str, user_id: str, account_id: str, *,
        image_data: Optional[bytes] = None, image_mime_type: str = "image/png",
        duration: Optional[int] = None, resolution: Optional[str] = None,
        aspect_ratio: Optional[str] = None, session_id: Optional[str] = None,
        origin_platform: Optional[str] = None,
    ) -> str:
        """
        Submit text-to-video or image-to-video (image_data present => image-to-video).

        Arranges delivery (enqueues the first poll tick) as a side effect, mirroring
        ClaudeDeepResearchAdapter.create_interaction() triggering its Cloud Run Job.
        origin_platform is carried through to delivery so the finished video reaches
        the same channel the request came from, not the user's primary/last-active
        channel — see WorkerHandler._handle_video_generation_polling.
        Returns request_id.
        """

    @abstractmethod
    async def edit_video(
        self, prompt: str, video_data: bytes, user_id: str, account_id: str, *,
        video_mime_type: str = "video/mp4", session_id: Optional[str] = None,
        origin_platform: Optional[str] = None,
    ) -> str:
        """
        Submit a video edit — modifies an existing video via prompt, preserving the
        rest of the scene. No duration/resolution params: editing preserves the
        source video's existing length/resolution. Same delivery-arrangement
        contract as create_video(), including origin_platform.
        """

    @abstractmethod
    async def get_status(self, request_id: str) -> VideoPollResult:
        """
        Poll job status. On "done", downloads xAI's returned video.url into bytes
        internally — callers never handle a raw URL, keeping delivery byte-based
        like ImageGenerationPort.
        """
