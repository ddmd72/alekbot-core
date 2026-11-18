"""
Wire tests: To Do times at the Graph boundary (P16-F).

Boundary: aiohttp.ClientSession, recorded POST body. The request's {"dateTime", "timeZone"}
is read the way Graph reads it (a wall-clock in the named zone), so the assertion is about
the instant the user meant, not about a call.

Defects these tests pinned, now fixed:
- P16-D10: `_dt_payload` labelled any wall-clock "UTC" without converting an aware value.
- P16-D11 (owner decision O7(b)): the automatic reminder was 20:00 UTC; it is 20:00 user-local the
  evening before. `_derive_reminder` takes the user's zone (`TasksAgent` gets it as `user_timezone`),
  and the test feeds that zone through the new input.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import pytest

from src.adapters.microsoft.todo_adapter import MicrosoftToDoAdapter
from src.agents.tasks_agent import TasksAgent
from src.domain.integration import Integration
from src.domain.credential_key import CredentialKey
from src.domain.oauth_credential import OAuthCredential
from src.domain.provider_account import ProviderAccount
from src.domain.task import TaskCreate, TaskUserConfig
from src.composition.connector_registry import ConnectorRegistry

_UID = "u1"
_LIST = {"id": "L1", "displayName": "Tasks", "isOwner": True, "isShared": False}
_CET = timezone(timedelta(hours=1))
_BERLIN = ZoneInfo("Europe/Berlin")


class _FakeCreds:
    async def list_for_user(self, user_id, kind=None, *a, **k):
        return [await self.get(user_id, "")]

    async def list_keys(self, user_id, kind=None, *a, **k):
        return [CredentialKey(user_id=user_id, credential_id="")]

    async def get(self, user_id, credential_id=None, *a, **k):
        return OAuthCredential(
            user_id=user_id,
            account_id="acct-1",
            kind="microsoft_todo",
            integration=Integration.TASKS,
            account=ProviderAccount(realm="microsoft", subject="", label="u1@example.com"),
            access_token="tok",
            refresh_token="ref",
            token_expiry=datetime.now(timezone.utc) + timedelta(hours=1),
            scopes=["Tasks.ReadWrite"],
        )

    async def save(self, *a, **k):
        return None

    async def update(self, *a, **k):
        return True


class _FakeTaskConfig:
    async def get_config(self, *a, **k):
        return TaskUserConfig()

    async def save_config(self, *a, **k):
        return None

    async def set_primary_list_id_if_absent(self, *a, **k):
        return "L1"


class _Resp:
    def __init__(self, data: Dict[str, Any]):
        self.status, self.ok, self._d = 200, True, data

    async def json(self):
        return self._d

    async def text(self):
        return str(self._d)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    """Records POST JSON bodies; GET returns the list, POST echoes a minimal task."""

    def __init__(self):
        self.posts: List[Dict[str, Any]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, url, **kw):
        return _Resp({"value": [_LIST], **_LIST})

    def post(self, url, json=None, **kw):
        self.posts.append(json)
        return _Resp({"id": "t1", "title": json.get("title", ""), "status": "notStarted"})


def _adapter() -> MicrosoftToDoAdapter:
    return MicrosoftToDoAdapter(
        oauth_credentials=_FakeCreds(), task_config=_FakeTaskConfig(),
        client_id="cid", client_secret="sec", connector=ConnectorRegistry().get("microsoft_todo"),
    )


async def _send(monkeypatch, task: TaskCreate) -> Dict[str, Any]:
    session = _Session()
    monkeypatch.setattr("aiohttp.ClientSession", lambda *a, **k: session)
    await _adapter().create_task(_UID, task)
    # The first POST may create the primary list ({"displayName"} only); the task is the one with a title.
    bodies = [b for b in session.posts if "title" in b]
    assert len(bodies) == 1
    return bodies[0]


def _instant(payload: Dict[str, str]) -> datetime:
    """Read a Graph dateTimeTimeZone as Graph does: wall-clock in the named zone."""
    zone = timezone.utc if payload["timeZone"] == "UTC" else ZoneInfo(payload["timeZone"])
    return datetime.fromisoformat(payload["dateTime"]).replace(tzinfo=zone)


_AT_0900_CET = datetime(2026, 11, 2, 9, 0, tzinfo=_CET)  # 08:00Z
_EXPECT_0800Z = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)


async def test_naive_utc_value_reaches_graph_as_same_utc_instant(monkeypatch):
    body = await _send(monkeypatch, TaskCreate(
        title="x", reminder_datetime=datetime(2026, 11, 2, 8, 0), is_reminder_on=True))
    assert _instant(body["reminderDateTime"]) == _EXPECT_0800Z


async def test_aware_reminder_denotes_same_instant(monkeypatch):
    body = await _send(monkeypatch, TaskCreate(
        title="x", reminder_datetime=_AT_0900_CET, is_reminder_on=True))
    assert _instant(body["reminderDateTime"]) == _EXPECT_0800Z


async def test_aware_due_denotes_same_instant(monkeypatch):
    body = await _send(monkeypatch, TaskCreate(title="x", due_datetime=_AT_0900_CET))
    assert _instant(body["dueDateTime"]) == _EXPECT_0800Z


async def test_aware_start_denotes_same_instant(monkeypatch):
    body = await _send(monkeypatch, TaskCreate(title="x", start_datetime=_AT_0900_CET))
    assert _instant(body["startDateTime"]) == _EXPECT_0800Z


async def test_agent_parsed_offset_string_denotes_same_instant(monkeypatch):
    parsed: Optional[datetime] = TasksAgent._parse_datetime("2026-11-02T09:00:00+01:00")
    body = await _send(monkeypatch, TaskCreate(
        title="x", reminder_datetime=parsed, is_reminder_on=True))
    assert _instant(body["reminderDateTime"]) == _EXPECT_0800Z


async def test_automatic_reminder_is_evening_before_in_user_zone(monkeypatch):
    due = TasksAgent._parse_datetime("2026-11-03")  # a date-only due: the user's zone comes from the agent
    reminder, on = TasksAgent._derive_reminder(None, due, _BERLIN)
    assert on is True
    body = await _send(monkeypatch, TaskCreate(
        title="x", due_datetime=due, reminder_datetime=reminder, is_reminder_on=on))
    expected = datetime(2026, 11, 2, 20, 0, tzinfo=_BERLIN).astimezone(timezone.utc)  # 19:00Z
    assert _instant(body["reminderDateTime"]) == expected
