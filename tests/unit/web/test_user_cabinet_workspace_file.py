"""The Cabinet shows a reminder's workspace file on one read-only line; it is edited through chat
(AGENT_WORKSPACE_RFC §8.3, §21 item 5)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from quart import Quart

from src.domain.agent_note import AgentNote
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from tests.support.cabinet_session_kit import cabinet_session_for
from src.composition.connector_registry import ConnectorRegistry
from tests.unit.web.cabinet_source import cabinet_source

_AUTH = {"Authorization": "Bearer token"}
DUE = datetime(2030, 1, 1, 6, tzinfo=timezone.utc)


def _app(notes):
    cabinet_sessions, _bearer = cabinet_session_for("u1", "a1")
    _AUTH.update(_bearer)
    users = MagicMock()
    users.get_user = AsyncMock(return_value=SimpleNamespace(config=SimpleNamespace(timezone="UTC")))
    notes_port = AsyncMock()
    notes_port.list_active_notes = AsyncMock(return_value=notes)
    app = Quart("test_reminder_workspace_file")
    app.register_blueprint(create_user_cabinet_blueprint(
        invite_service=MagicMock(), cabinet_sessions=cabinet_sessions, user_repo=users, fact_repo=MagicMock(),
        embedding_service=MagicMock(), agent_note_port=notes_port, connectors=ConnectorRegistry(),
    ))
    return app


def _note(note_id, **kw):
    return AgentNote(note_id=note_id, user_id="u1", text="Morning briefing", instruction="x",
                     created_at=DUE - timedelta(days=1), due=DUE, **kw)


async def test_get_returns_the_field():
    notes = [_note("1729401234567", workspace_file="projects/morning-briefing.md"), _note("2")]
    async with _app(notes).test_client() as client:
        resp = await client.get("/api/user/reminders", headers=_AUTH)
    assert resp.status_code == 200
    bound, plain = (await resp.get_json())["reminders"]
    assert bound["workspace_file"] == "projects/morning-briefing.md"
    assert plain["workspace_file"] is None


def test_the_list_shows_it_escaped_on_one_read_only_line():
    page = cabinet_source()
    assert "workspace file: workspace:${esc(r.workspace_file)}" in page
    assert "${fileLine}" in page
    assert "rem-workspace-file" not in page  # no form field: chat edits it
