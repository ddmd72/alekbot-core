"""
video_generation_delivery.py — deliver a finished video to the user.

Structural sibling of deep_research_delivery.py::deliver_deep_research(), but
simpler: video bytes are already resolved (GrokVideoAdapter.get_status()
downloads them inline), so there's no "upload multiple rounds + build an HTML
report" step — just one GCS upload + one notify_document_link() call, plus the
billing call deep research's own delivery path does not have.

See docs/10_rfcs/VIDEO_GENERATION_RFC.md §3.4 step 4, §3.11 decision #11.
"""
import datetime
from typing import Optional

from ..domain.billing import calculate_external_cost
from ..utils.logger import logger


async def deliver_video(
    video_data: bytes,
    user_id: str,
    account_id: str,
    duration_s: int,
    media_storage,       # MediaStoragePort
    notification,        # NotificationPort (deep_research_delivery.py's Protocol)
    quota_service=None,  # QuotaService, Optional — None-guarded, same contract as ImageGenerationAgent
    link_service=None,   # FileLinkService, Optional
    channel_id_override: Optional[str] = None,
    platform_override: Optional[str] = None,
) -> None:
    """
    Deliver a finished video:
      1. Upload bytes to GCS.
      2. Send a named link to the user via notify_document_link (same call deep
         research already uses for its report links).
      3. Record the actual delivered cost via QuotaService.record_usage() — this
         is where duration_s is finally KNOWN (resolved + clamped by
         VideoGenerationAgent, carried through the poll payload).
    """
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    key = f"video_generation/{user_id}/{timestamp}.mp4"

    try:
        await media_storage.store(data=video_data, key=key, content_type="video/mp4")
    except Exception as exc:
        logger.error("[VideoGeneration] GCS upload failed: %s", exc, exc_info=True)
        return

    url = key
    if link_service:
        try:
            url = link_service.build_link(key=key, user_id=user_id)
        except Exception as exc:
            logger.error("[VideoGeneration] build_link failed, falling back to key: %s", exc, exc_info=True)
            url = key

    try:
        await notification.notify_document_link(
            user_id=user_id, account_id=account_id,
            url=url, label="Your generated video", key=key,
            channel_id_override=channel_id_override,
            platform_override=platform_override,
        )
    except Exception as exc:
        logger.error("[VideoGeneration] notify_document_link failed: %s", exc, exc_info=True)

    if quota_service:
        try:
            cost = calculate_external_cost("grok-imagine-video-1.5", duration_s=duration_s)
            await quota_service.record_usage(
                account_id=account_id, model="grok-imagine-video-1.5", tokens=0, cost=cost,
            )
        except Exception as exc:
            logger.error("[VideoGeneration] record_usage failed: %s", exc, exc_info=True)
