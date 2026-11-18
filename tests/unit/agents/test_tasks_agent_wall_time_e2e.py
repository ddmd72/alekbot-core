"""Tasks: a time of day the model sends without an offset is the user's wall time, read where Graph receives it.

The model writes the user's own clock ("remind me at 18:00" → `2026-11-03T18:00:00`); the adapter labels every time
UTC, so the agent turns such a time into its instant in the user's zone first (`wall_time_instant`, the reading notes
and the Cabinet reminders already use). A date alone has no instant and stays the day as written; a time with an offset
is its own instant. Same rig as `test_tasks_agent_time_fields_e2e.py`: the real agent behind a scripted model, the real
To Do adapter on the aiohttp boundary fake.
"""
from datetime import datetime, timezone

from tests.support.clock_instants import BERLIN, NEW_YORK, SKIPPED_HOUR
from tests.support.scripted_llm import text, tools
from tests.unit.agents.test_tasks_agent_time_fields_e2e import REF, TASKS_URL, Rig, _instant


async def test_a_naive_reminder_on_create_is_the_users_wall_time(monkeypatch):
    """Flips when a naive time is sent as UTC (18:00Z instead of 17:00Z in Berlin, CET)."""
    rig = await Rig([tools(("create_task", {"title": "Car service", "due_datetime": "2026-11-04",
                                            "reminder_datetime": "2026-11-03T18:00:00"})),
                     text("Created.")], monkeypatch, zone=BERLIN).start()

    await rig.run()

    (post,) = rig.sent("POST", TASKS_URL)
    assert rig.finished()
    assert _instant(post.json["reminderDateTime"]) == datetime(2026, 11, 3, 17, 0, tzinfo=timezone.utc)
    assert post.json["isReminderOn"] is True


async def test_a_naive_due_and_start_with_a_time_are_the_users_wall_time(monkeypatch):
    """New York is on EST (UTC-5) after Nov 1: 09:00 there is 14:00Z."""
    rig = await Rig([tools(("create_task", {"title": "Car service", "due_datetime": "2026-11-04T09:00:00",
                                            "start_datetime": "2026-11-03T09:00:00"})),
                     text("Created.")], monkeypatch, zone=NEW_YORK).start()

    await rig.run()

    (post,) = rig.sent("POST", TASKS_URL)
    assert rig.finished()
    assert _instant(post.json["dueDateTime"]) == datetime(2026, 11, 4, 14, 0, tzinfo=timezone.utc)
    assert _instant(post.json["startDateTime"]) == datetime(2026, 11, 3, 14, 0, tzinfo=timezone.utc)
    assert _instant(post.json["reminderDateTime"]) == datetime(2026, 11, 4, 1, 0, tzinfo=timezone.utc)  # 20:00 EST


async def test_a_date_only_due_stays_the_day_as_written(monkeypatch):
    """A date names no instant: read in Berlin and sent as UTC it would land on the previous day."""
    rig = await Rig([tools(("create_task", {"title": "Car service", "due_datetime": "2026-11-04"})),
                     text("Created.")], monkeypatch, zone=BERLIN).start()

    await rig.run()

    (post,) = rig.sent("POST", TASKS_URL)
    assert rig.finished()
    assert _instant(post.json["dueDateTime"]) == datetime(2026, 11, 4, 0, 0, tzinfo=timezone.utc)


async def test_a_time_with_an_offset_keeps_its_instant_whatever_the_zone(monkeypatch):
    rig = await Rig([tools(("create_task", {"title": "Car service", "due_datetime": "2026-11-04T09:00:00+01:00",
                                            "reminder_datetime": "2026-11-03T18:00:00Z"})),
                     text("Created.")], monkeypatch, zone=NEW_YORK).start()

    await rig.run()

    (post,) = rig.sent("POST", TASKS_URL)
    assert rig.finished()
    assert _instant(post.json["dueDateTime"]) == datetime(2026, 11, 4, 8, 0, tzinfo=timezone.utc)
    assert _instant(post.json["reminderDateTime"]) == datetime(2026, 11, 3, 18, 0, tzinfo=timezone.utc)


async def test_a_naive_reminder_on_update_is_the_users_wall_time(monkeypatch):
    rig = await Rig([tools(("update_task", {"task_ref": REF, "reminder_datetime": "2026-11-03T18:00:00"})),
                     text("Updated.")], monkeypatch, zone=BERLIN).start()
    await rig.seed()

    await rig.run()

    (patch,) = [r.json for r in rig.sent("PATCH", f"{TASKS_URL}/")]
    assert rig.finished()
    assert _instant(patch["reminderDateTime"]) == datetime(2026, 11, 3, 17, 0, tzinfo=timezone.utc)


async def test_a_wall_time_the_clocks_skip_is_the_tools_error_and_nothing_reaches_graph(monkeypatch):
    skipped = SKIPPED_HOUR[BERLIN].naive.isoformat(timespec="minutes")
    rig = await Rig([tools(("create_task", {"title": "Car service", "reminder_datetime": skipped})),
                     text("Could not create it.")], monkeypatch, zone=BERLIN).start()

    await rig.run()

    (result,) = rig.results_of("create_task")
    assert rig.finished()
    assert rig.sent("POST", TASKS_URL) == []
    assert "error" in result and BERLIN in result["error"]
