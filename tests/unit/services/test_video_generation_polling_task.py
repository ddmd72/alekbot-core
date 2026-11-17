"""VideoGenerationPollingTask: one poll of a video job, over a real provider registry and recording fakes."""
import logging
from typing import Any, Dict, List, Optional

import pytest

from src.domain.notification_kind import NotificationKind
from src.ports.video_generation_port import VideoPollResult
from src.services.provider_registry import ProviderRegistry
from src.services.video_generation_polling_task import VideoGenerationPollingTask

PAYLOAD = {"request_id": "req-1", "user_id": "user-1", "account_id": "acct-1", "session_id": "user-1:chan-1",
           "attempt": 0}


class ScriptedVideo:
    def __init__(self, result: Optional[VideoPollResult] = None, raises: Optional[Exception] = None) -> None:
        self._result = result
        self._raises = raises
        self.polled: List[str] = []

    async def get_status(self, request_id: str) -> VideoPollResult:
        self.polled.append(request_id)
        if self._raises is not None:
            raise self._raises
        assert self._result is not None
        return self._result


class Rig:
    def __init__(self, result: Optional[VideoPollResult] = None, raises: Optional[Exception] = None,
                 provider: str = "grok") -> None:
        self.order: List[str] = []
        self.video = ScriptedVideo(result, raises)
        self.registry: ProviderRegistry = ProviderRegistry()
        self.registry.register(provider, self.video)
        self.enqueued: List[Dict[str, Any]] = []
        self.notified: List[Dict[str, Any]] = []
        self.delivered: List[Dict[str, Any]] = []
        self.media, self.quota, self.links, self.short_links = object(), object(), object(), object()
        self.task = VideoGenerationPollingTask(
            video_registry=self.registry, task_dispatch=self, ensure_agents=self._ensure, notification_service=self,
            media_storage=self.media, quota_service=self.quota, link_service=self.links,
            short_link_service=self.short_links, deliver_video=self._deliver)

    async def _ensure(self, user_id: str) -> None:
        self.order.append(f"ensure:{user_id}")

    async def enqueue_video_generation_polling(self, **kwargs: Any) -> None:
        self.enqueued.append(kwargs)

    async def notify(self, **kwargs: Any) -> None:
        self.order.append("notify")
        self.notified.append(kwargs)

    async def _deliver(self, **kwargs: Any) -> None:
        self.delivered.append(kwargs)

    async def run(self, **payload: Any) -> Any:
        return await self.task({**PAYLOAD, **payload}, {})


async def test_a_provider_that_is_not_registered_answers_500_and_names_the_available_ones(caplog):
    rig = Rig()
    with caplog.at_level(logging.ERROR):
        assert await rig.run(provider="other") == ({"error": "video_generation not configured"}, 500)
    assert "provider='other', available=['grok']" in caplog.text
    assert rig.video.polled == [] and rig.order == []


async def test_the_agents_are_built_before_a_notice_and_only_for_a_known_user():
    rig = Rig(VideoPollResult(status="failed", error="bad"))
    await rig.run()
    assert rig.order == ["ensure:user-1", "notify"]

    nobody = Rig(VideoPollResult(status="pending"))
    await nobody.run(user_id="")
    assert nobody.order == []


async def test_past_the_attempt_budget_the_user_is_told_and_nothing_is_polled():
    rig = Rig()
    attempt = VideoGenerationPollingTask._MAX_VIDEO_POLL_ATTEMPTS
    assert await rig.run(attempt=attempt, origin_platform="slack") == ({"status": "timeout"}, 200)
    assert rig.notified == [dict(user_id="user-1", account_id="acct-1", kind=NotificationKind.DEEP_RESEARCH,
                                 system_alert="Video generation timed out without producing a result.",
                                 channel_id_override="chan-1", platform_override="slack")]
    assert rig.video.polled == [] and rig.enqueued == []


async def test_the_last_attempt_inside_the_budget_still_polls():
    rig = Rig(VideoPollResult(status="pending"))
    attempt = VideoGenerationPollingTask._MAX_VIDEO_POLL_ATTEMPTS - 1
    assert await rig.run(attempt=attempt) == ({"status": "polling", "attempt": attempt + 1}, 200)


async def test_a_poll_that_raises_is_enqueued_again_with_the_next_attempt(caplog):
    rig = Rig(raises=RuntimeError("blip"))
    with caplog.at_level(logging.WARNING):
        assert await rig.run(attempt=2, duration_s=8, origin_platform="telegram") == (
            {"status": "retry", "attempt": 3}, 200)
    assert rig.enqueued == [dict(request_id="req-1", user_id="user-1", account_id="acct-1",
                                 session_id="user-1:chan-1", attempt=3, delay_seconds=30, duration_s=8,
                                 origin_platform="telegram")]
    assert "get_status failed, attempt=2: blip" in caplog.text
    assert rig.notified == []


async def test_a_pending_job_is_enqueued_again_with_its_duration_and_platform_threaded_forward():
    rig = Rig(VideoPollResult(status="pending"))
    assert await rig.run(attempt=4, duration_s=7, origin_platform="slack") == ({"status": "polling", "attempt": 5}, 200)
    assert rig.enqueued == [dict(request_id="req-1", user_id="user-1", account_id="acct-1",
                                 session_id="user-1:chan-1", attempt=5, delay_seconds=30, duration_s=7,
                                 origin_platform="slack")]


async def test_a_missing_duration_falls_back_to_five_seconds_in_the_next_poll():
    rig = Rig(VideoPollResult(status="pending"))
    await rig.run()
    assert rig.enqueued[0]["duration_s"] == 5 and rig.enqueued[0]["origin_platform"] is None


async def test_a_finished_video_is_delivered_with_the_runners_collaborators_to_the_origin_channel():
    rig = Rig(VideoPollResult(status="done", data=b"bytes", duration_s=12))
    assert await rig.run(duration_s=5, origin_platform="slack") == ({"status": "delivered"}, 200)
    assert rig.delivered == [dict(video_data=b"bytes", user_id="user-1", account_id="acct-1", duration_s=12,
                                  media_storage=rig.media, notification=rig, quota_service=rig.quota,
                                  link_service=rig.links, short_link_service=rig.short_links,
                                  channel_id_override="chan-1", platform_override="slack")]
    assert rig.enqueued == [] and rig.notified == []


async def test_the_duration_the_provider_did_not_report_comes_from_the_payload():
    rig = Rig(VideoPollResult(status="done", data=b"bytes"))
    await rig.run(duration_s=6)
    assert rig.delivered[0]["duration_s"] == 6


async def test_a_session_without_a_colon_has_no_origin_channel():
    rig = Rig(VideoPollResult(status="done", data=b"bytes"))
    await rig.run(session_id="user-1")
    assert rig.delivered[0]["channel_id_override"] is None


@pytest.mark.parametrize("status", ["failed", "expired"])
async def test_a_failed_job_tells_the_user_the_provider_reason_and_is_not_polled_again(status, caplog):
    rig = Rig(VideoPollResult(status=status, error="Video is too long."))
    with caplog.at_level(logging.ERROR):
        assert await rig.run(origin_platform="telegram") == ({"status": status}, 200)
    assert rig.notified == [dict(user_id="user-1", account_id="acct-1", kind=NotificationKind.DEEP_RESEARCH,
                                 system_alert="Video generation did not complete — the AI provider returned an "
                                              "error. Reason: Video is too long.",
                                 channel_id_override="chan-1", platform_override="telegram")]
    assert rig.enqueued == [] and rig.delivered == []
    assert f"[VideoGeneration] {status}: request=req-1, error=Video is too long." in caplog.text


async def test_a_failed_job_without_a_reason_gets_the_generic_notice_only():
    rig = Rig(VideoPollResult(status="failed", error=""))
    await rig.run()
    assert rig.notified[0]["system_alert"] == "Video generation did not complete — the AI provider returned an error."
