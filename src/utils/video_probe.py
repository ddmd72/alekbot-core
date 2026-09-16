"""
video_probe.py — probe a video's duration from raw bytes via ffprobe.

Single-purpose, single-implementation local-tool wrapper — not a port. No
substitution need (ffprobe is a fixed system binary, installed via the
Dockerfile alongside ffmpeg), used from exactly one call site
(VideoGenerationAgent._execute_edit, enforcing xAI's edit_video source-video
length cap — see agent_manifest.py's EDIT_VIDEO capability_description).
"""
import asyncio
import tempfile
from typing import Optional

from .logger import logger

_PROBE_TIMEOUT_S = 15.0


async def probe_video_duration_s(video_data: bytes) -> Optional[float]:
    """
    Return the video's duration in seconds, or None if ffprobe fails, times
    out, or is unavailable. Callers must treat None as "unknown" — never
    block on a probing failure, since that would reject an otherwise-valid
    video the real API might still accept.
    """
    with tempfile.NamedTemporaryFile(suffix=".mp4") as tmp:
        tmp.write(video_data)
        tmp.flush()
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", tmp.name,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_PROBE_TIMEOUT_S)
        except Exception as exc:
            logger.warning("[VideoProbe] ffprobe invocation failed: %s", exc)
            return None

    if proc.returncode != 0:
        logger.warning(
            "[VideoProbe] ffprobe exited %s: %s",
            proc.returncode, stderr.decode(errors="replace")[:200],
        )
        return None
    try:
        return float(stdout.decode().strip())
    except ValueError:
        logger.warning("[VideoProbe] ffprobe returned non-numeric duration: %r", stdout)
        return None
