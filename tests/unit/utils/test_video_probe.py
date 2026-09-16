"""
Unit tests for video_probe.py.

Mock boundary: asyncio.create_subprocess_exec (subprocess layer), same
convention as tests/unit/adapters/test_node_puppeteer_runner.py.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.utils.video_probe import probe_video_duration_s


def _mock_proc(returncode=0, stdout=b"", stderr=b""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


@pytest.mark.asyncio
async def test_returns_duration_on_success():
    proc = _mock_proc(returncode=0, stdout=b"8.733333\n")
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await probe_video_duration_s(b"fake-video-bytes")

    assert result == pytest.approx(8.733333)


@pytest.mark.asyncio
async def test_returns_none_on_nonzero_exit():
    proc = _mock_proc(returncode=1, stderr=b"Invalid data found when processing input")
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await probe_video_duration_s(b"not-a-real-video")

    assert result is None


@pytest.mark.asyncio
async def test_returns_none_on_non_numeric_stdout():
    proc = _mock_proc(returncode=0, stdout=b"N/A\n")
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await probe_video_duration_s(b"fake-video-bytes")

    assert result is None


@pytest.mark.asyncio
async def test_returns_none_when_ffprobe_missing():
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=FileNotFoundError("ffprobe"))):
        result = await probe_video_duration_s(b"fake-video-bytes")

    assert result is None


@pytest.mark.asyncio
async def test_returns_none_on_timeout():
    proc = _mock_proc(returncode=0)
    proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await probe_video_duration_s(b"fake-video-bytes")

    assert result is None
