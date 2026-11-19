"""The `geo_mark_turn` worker task (GEO_MARKS_RFC §4.5): it refuses what it cannot run and hands the frozen payload to
the platform's turn runner (its ten keys, as sent); composition registers it whatever is configured."""
import pytest

from src.composition.worker_setup import build_worker_tasks
from src.services.geo_mark_turn_task import PAYLOAD_KEYS, GeoMarkTurnTask

TURN = {"platform": "slack", "user_id": "usr-1", "account_id": "acc-1", "channel_id": "C1", "session_id": "usr-1:C1",
        "platform_user_id": "U1", "text": "Is the Iron Temple gym open?", "mark_text": "[Geo mark: …]", "language": "en",
        "link_fp": "0a1b2c3d"}
PAYLOAD = {"task_type": "geo_mark_turn", **TURN}
HEADERS = {"X-CloudTasks-TaskName": "geo_mark_turn-1"}


class Runner:
    def __init__(self) -> None:
        self.runs = []

    async def __call__(self, payload, headers):
        self.runs.append((payload, headers))
        return {"status": "ok"}, 200


async def test_without_a_runner_it_answers_501():
    assert await GeoMarkTurnTask(run_turn=None)(dict(PAYLOAD), HEADERS) == (
        {"error": "geo mark turns not configured"}, 501)


async def test_the_runner_gets_the_ten_keys_as_sent_and_the_headers():
    runner = Runner()

    answer = await GeoMarkTurnTask(run_turn=runner)(dict(PAYLOAD), HEADERS)

    assert answer == ({"status": "ok"}, 200)
    assert runner.runs == [(TURN, HEADERS)]


async def test_an_empty_text_and_mark_are_still_strings_and_run():
    runner = Runner()
    await GeoMarkTurnTask(run_turn=runner)({**PAYLOAD, "text": ""}, HEADERS)
    assert len(runner.runs) == 1


@pytest.mark.parametrize("key", PAYLOAD_KEYS)
async def test_a_missing_or_non_string_key_is_400_and_runs_nothing(key):
    runner = Runner()
    task = GeoMarkTurnTask(run_turn=runner)

    missing = await task({k: v for k, v in PAYLOAD.items() if k != key}, HEADERS)
    wrong = await task({**PAYLOAD, key: 7}, HEADERS)

    assert missing == wrong == ({"error": "invalid geo_mark_turn payload"}, 400)
    assert runner.runs == []


async def test_another_platform_is_400_and_runs_nothing():
    runner = Runner()
    assert await GeoMarkTurnTask(run_turn=runner)({**PAYLOAD, "platform": "telegram"}, HEADERS) == (
        {"error": "unsupported platform"}, 400)
    assert runner.runs == []


def _deps(**overrides):
    async def ensure(user_id):
        return None

    deps = dict(notes_port=None, user_repo=object(), ensure_agents=ensure, notification_service=object(),
                alert_sink=None, task_dispatch=None, video_registry=None, quota_service=None, media_storage=None,
                link_service=None, short_link_service=None, email_review=None, daily_review_marks=None,
                gmail_reconnect_flag=object(), account_repo=None)
    deps.update(overrides)
    return deps


async def test_composition_registers_the_task_with_the_given_runner():
    runner = Runner()

    task = build_worker_tasks(**_deps(geo_mark_turn=runner))["geo_mark_turn"]

    assert type(task) is GeoMarkTurnTask
    assert await task(dict(PAYLOAD), HEADERS) == ({"status": "ok"}, 200) and runner.runs == [(TURN, HEADERS)]


async def test_composition_without_a_runner_registers_the_501_task():
    task = build_worker_tasks(**_deps())["geo_mark_turn"]
    assert type(task) is GeoMarkTurnTask
    assert (await task(dict(PAYLOAD), HEADERS))[1] == 501
