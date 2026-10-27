"""P13 card B: the `/worker` paths that route a notice by the origin of a background job (deep research failure, the
OpenAI webhook's failed event, the Grok video poll) reach that origin, never the primary, and keep the history pair in
the origin's session.

Every test runs on `SeamWorld` (tests/support/seam_world.py): the real `WorkerHandler`, `UserNotificationService`,
`AgentCoordinator`, `TaskDispatchService`, `GcpTaskQueue`, `GcsMediaAdapter`, `FileLinkService` and the session store;
the quick formatter is a scripted stand-in, the video provider and the OpenAI retrieval are scripted boundaries. The
primary is `("slack", "C1")` in every test, the origin `("telegram", "7001")`: a keyword dropped at a forward lands the
notice on the primary instead (`ums.py:119-137`), which the assertions on C1 catch.
"""
from typing import List, Optional

from src.domain import deep_research_notices as notices
from src.ports.video_generation_port import VideoGenerationPort, VideoPollResult
from src.services.provider_registry import ProviderRegistry
from tests.support.seam_world import ACCOUNT, PRIMARY, TELEGRAM, USER, SeamWorld, require, says

QUERY = "Heat pumps in Springfield"
TOO_LONG = "Video is too long. Maximum duration is 8.7 seconds."


class ScriptedVideo(VideoGenerationPort):
    """The video provider boundary: `get_status` answers the scripted results in order and records the request ids.
    Submission is not on these paths (the poll payload is delivered directly)."""

    def __init__(self, *results: VideoPollResult) -> None:
        self._results: List[VideoPollResult] = list(results)
        self.polled: List[str] = []

    async def create_video(self, prompt, user_id, account_id, **kwargs) -> str:  # type: ignore[override]
        raise RuntimeError("create_video is not on the poll path")

    async def edit_video(self, prompt, video_data, user_id, account_id, **kwargs) -> str:  # type: ignore[override]
        raise RuntimeError("edit_video is not on the poll path")

    async def get_status(self, request_id: str) -> VideoPollResult:
        self.polled.append(request_id)
        if not self._results:
            raise RuntimeError("no scripted poll result")
        return self._results.pop(0)


def _video_registry(video: ScriptedVideo) -> ProviderRegistry:
    registry: ProviderRegistry = ProviderRegistry()
    registry.register("grok", video)
    return registry


def _fetch(response: dict):
    """The OpenAI retrieval boundary (`worker_handler.py:648`): a coroutine of the job id."""
    fetched: List[str] = []

    async def fetch(job_id: str) -> dict:
        fetched.append(job_id)
        return response

    fetch.fetched = fetched  # type: ignore[attr-defined]
    return fetch


def _failed_payload(origin_platform: Optional[str] = "telegram") -> dict:
    return {"task_type": "deep_research_failed", "user_id": USER, "account_id": ACCOUNT, "session_id": "u1:7001",
            "origin_platform": origin_platform, "query": QUERY, "reason": "agent_failed"}


def _poll_payload(attempt: int = 0) -> dict:
    return {"task_type": "video_generation_polling", "request_id": "req-1", "user_id": USER, "account_id": ACCOUNT,
            "session_id": "u1:7001", "origin_platform": "telegram", "attempt": attempt, "duration_s": 6}


def _alert_of(message) -> str:
    return message.context["current_message_parts"][0].text


async def test_a_failed_deep_research_job_tells_the_chat_it_came_from(monkeypatch):
    """Guards: `worker_handler.py:561` (origin channel from `session_id`) and `:571` (`origin_platform`) →
    `_send_deep_research_notice` `:604,605` → `ums.py:119-125` (the override) → `:350-352`; the history session
    `ums.py:376,447-449`. Dropping either keyword sends the notice to the primary C1 and its pair to `u1:C1`."""
    world = SeamWorld(monkeypatch)
    await world.set_primary(*PRIMARY)
    world.agent("quick_response_agent").play(says("Your deep research did not finish; you can ask for it again."))

    answer = await world.run(_failed_payload())

    require(not world.unscripted(), "every agent call was scripted")
    assert answer == ({"status": "ok"}, 200)
    assert [(p.kind, p.value) for p in world.posts_to(*TELEGRAM)] == [
        ("text", "Your deep research did not finish; you can ask for it again.")]
    assert world.posts_to(*PRIMARY) == []
    assert world.history("u1:7001") == [
        ("user", notices.research_failed_notice(QUERY), None),
        ("model", "Your deep research did not finish; you can ask for it again.",
         "Your deep research did not finish; you can ask for it again."),
    ]
    assert world.history("u1:C1") == []


async def test_a_failed_webhook_event_tells_the_chat_it_came_from(monkeypatch):
    """Guards: `worker_handler.py:643-644` (origin channel and platform of the webhook event) → `:697-699` →
    `_send_deep_research_notice` `:604-605` → `ums.py:119-125`; session `ums.py:376,447-449`."""
    fetch = _fetch({"error": {"message": "x"}})
    world = SeamWorld(monkeypatch, deep_research_fetch=fetch)
    await world.set_primary(*PRIMARY)
    world.agent("quick_response_agent").play(says("The research provider failed; you can ask again."))

    answer = await world.run({
        "task_type": "deep_research_webhook_event", "event_type": "response.failed", "job_id": "resp_1",
        "user_id": USER, "account_id": ACCOUNT, "session_id": "u1:7001", "origin_platform": "telegram",
        "query": QUERY,
    })

    require(not world.unscripted(), "every agent call was scripted")
    require(fetch.fetched == ["resp_1"], "the job was retrieved by its id")
    assert answer == ({"status": "ok"}, 200)
    assert [(p.kind, p.value) for p in world.posts_to(*TELEGRAM)] == [
        ("text", "The research provider failed; you can ask again.")]
    assert world.posts_to(*PRIMARY) == []
    assert world.history("u1:7001") == [
        ("user", notices.provider_error_notice(QUERY), None),
        ("model", "The research provider failed; you can ask again.",
         "The research provider failed; you can ask again."),
    ]
    assert world.history("u1:C1") == []


async def test_a_notice_with_no_origin_platform_goes_to_the_primary(monkeypatch):
    """Control, by design: a payload from before the platform travelled (`origin_platform` empty; the webhook route
    notes it is "absent on pre-deploy jobs", `deep_research_webhooks.py:174`) falls back to the primary
    (`worker_handler.py:571` → `None` → `ums.py:119` not taken → `:129-131`). Fails if the handler starts inventing a
    platform for the origin channel."""
    world = SeamWorld(monkeypatch)
    await world.set_primary(*PRIMARY)
    world.agent("quick_response_agent").play(says("Your deep research did not finish."))

    answer = await world.run(_failed_payload(origin_platform=""))

    require(not world.unscripted(), "every agent call was scripted")
    assert answer == ({"status": "ok"}, 200)
    assert [(p.kind, p.value) for p in world.posts_to(*PRIMARY)] == [("text", "Your deep research did not finish.")]
    assert world.posts_to(*TELEGRAM) == []
    assert [role for role, _, _ in world.history("u1:C1")] == ["user", "model"]
    assert world.history("u1:7001") == []


async def test_a_failed_video_render_is_reported_where_it_was_asked(monkeypatch):
    """Guards: `worker_handler.py:733` (`origin_platform`) and `:735` (origin channel from `session_id`) →
    `:827-828` → `ums.py:119-125`; the provider's reason `:821-822` reaches the formatter."""
    video = ScriptedVideo(VideoPollResult("failed", error=TOO_LONG))
    world = SeamWorld(monkeypatch, video_registry=_video_registry(video))
    await world.set_primary(*PRIMARY)
    quick = world.agent("quick_response_agent").play(says("The video was too long: 8.7 seconds at most."))

    answer = await world.run(_poll_payload())

    require(not world.unscripted(), "every agent call was scripted")
    require(video.polled == ["req-1"], "the request was polled once")
    assert answer == ({"status": "failed"}, 200)
    assert [(p.kind, p.value) for p in world.posts_to(*TELEGRAM)] == [
        ("text", "The video was too long: 8.7 seconds at most.")]
    assert world.posts_to(*PRIMARY) == []
    [message] = quick.received
    assert f"Reason: {TOO_LONG}" in _alert_of(message)
    assert message.context["session_id"] == "u1:7001"


async def test_a_pending_video_is_polled_again_and_delivered_to_its_chat(monkeypatch):
    """Guards, the re-poll: `worker_handler.py:780-784` (`session_id`, `attempt`, `duration_s`, `origin_platform`) →
    `tds:62-71` → `gcp:267-276` (the body) and `:292-297` (the schedule). The delivery: `:806-807` →
    `video_generation_delivery.py:65-70` → `ums.py:504-506` (the channel) and `:550` (the session); the key
    `video_generation_delivery.py:67` → `ums.py:528-536` (the video note)."""
    video = ScriptedVideo(VideoPollResult("pending"), VideoPollResult("done", data=b"\x00\x01", duration_s=6))
    world = SeamWorld(monkeypatch, video_registry=_video_registry(video))
    await world.set_primary(*PRIMARY)

    first = await world.run(_poll_payload(attempt=0))

    require(first == ({"status": "polling", "attempt": 1}, 200), "the pending poll was answered 'polling'")
    [request] = world.requests("video_generation_polling")
    assert request.scheduled is True
    assert request.body == {
        "task_type": "video_generation_polling", "request_id": "req-1", "user_id": USER, "account_id": ACCOUNT,
        "session_id": "u1:7001", "duration_s": 6, "origin_platform": "telegram", "attempt": 1,
    }
    require(world.channels.posts == [], "nothing was posted while pending")

    second = await world.run(request.body)

    require(second == ({"status": "delivered"}, 200), "the done poll was answered 'delivered'")
    require(video.polled == ["req-1", "req-1"], "the request was polled twice")
    require(not world.unscripted(), "no agent was called")
    [(label, url)] = world.docs_to(*TELEGRAM)
    assert label == "Your generated video"
    assert url.startswith("https://svc.test.example/f/")
    assert world.posts_to(*TELEGRAM) == world.channels.posts
    assert world.posts_to(*PRIMARY) == []
    [key] = world.stored_keys()
    assert key.startswith("video_generation/u1/") and key.endswith(".mp4")
    [(_, user_text, _), (_, note, _)] = world.history("u1:7001")
    assert user_text == "[System: async document ready — Your generated video]"
    assert note.startswith("[Video delivered to user: Your generated video.")
    assert f'"file_ref": "{key}"' in note
    assert world.history("u1:C1") == []
