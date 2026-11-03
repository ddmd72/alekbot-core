"""One DriveOperations per user (DRIVE_SPECIALIST_RFC §5.5): the factory builds it on first use, keeps it in the
user's context and hands the same instance to every agent that touches the drive, so the per-user mutation lock, the
shielded tasks and the late-failure notices are one set per user. A rebuilt user context (cache eviction, TTL) gets
a fresh one."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.agents.file_management_agent import FileManagementAgent
from src.composition.user_agent_factory import UserAgentFactory, _UserContext
from src.domain.document_presentation import present_text
from src.domain.document_read import ReadOutcome, ReadStatus
from src.domain.drive_search_line import SearchLine
from src.domain.language import LanguageCode
from src.domain.user_drive import DeleteOutcome, DriveItem
from src.infrastructure.drive_operations import DriveOperations
from tests.support.drive_service_spec_mock import spec_drive_service
from tests.support.opened_file import opened_body


def _ctx() -> _UserContext:
    profile = MagicMock()
    profile.config.timezone = "Europe/Madrid"
    return _UserContext(user_profile=profile, prompt_builder=MagicMock())


def _factory(**over) -> SimpleNamespace:
    fields = dict(file_conversion_service=MagicMock(), file_storage=MagicMock(), notification_service=MagicMock(),
                  user_drive_service=MagicMock(), localization=MagicMock(), language_service=MagicMock(),
                  drive_search_service=MagicMock(), drive_note_service=None)
    fields.update(over)
    fake = SimpleNamespace(**fields)
    fake._drive_operations = lambda ctx: UserAgentFactory._drive_operations(fake, ctx)
    return fake


def test_the_drive_operations_are_built_once_per_user_context():
    fake, ctx = _factory(), _ctx()
    first = UserAgentFactory._drive_operations(fake, ctx)
    assert isinstance(first, DriveOperations) and ctx.drive_operations is first
    assert UserAgentFactory._drive_operations(fake, ctx) is first


async def test_every_shared_service_reaches_the_instance(monkeypatch):
    """DriveOperations keeps its services private: each one is observed through the call it makes."""
    monkeypatch.setattr("src.infrastructure.drive_operations.secrets.token_hex", lambda n: "0123456789abcdef")
    # the drive service's own connection reads (mounts, resolve_ref): the ref resolves on a real mount table
    fake, ctx = _factory(user_drive_service=spec_drive_service("user-1")), _ctx()
    fake.user_drive_service.status.side_effect = RuntimeError("offline")
    fake.user_drive_service.delete.return_value = DeleteOutcome(
        item=DriveItem(item_id="item-1", name="a.txt", path="Inbox/a.txt", is_folder=False), file_count=1)
    fake.drive_search_service.search_state = AsyncMock(return_value=SearchLine.REBUILDING)
    fake.file_conversion_service.open = AsyncMock(return_value=opened_body("chat file text"))
    fake.language_service.resolve_ui_language = AsyncMock(return_value=LanguageCode.EN)
    fake.localization.get_ui_string = MagicMock(return_value="Deleted {path}")
    fake.notification_service.notify_raw = AsyncMock()
    ops = UserAgentFactory._drive_operations(fake, ctx)

    preflight = await ops.preflight("user-1")                       # the drive service and the drive search
    assert preflight.search is SearchLine.REBUILDING
    fake.user_drive_service.status.assert_awaited_once_with("user-1")
    opened = await ops.fetch_stored({}, "user-1", "notes.txt")      # the conversion service
    # a chat file comes marked (EXTERNAL_FILES_RFC P15): the conversion service's text inside the neutral wrapper
    assert opened.text == present_text("chat file text", store="chat", ref="notes.txt", label="notes.txt",
                                       nonce="0123456789abcdef", outcome=ReadOutcome(status=ReadStatus.TEXT))
    assert fake.file_conversion_service.open.await_args.args == ("notes.txt", "user-1", "")
    await ops.delete({"account_id": "acc"}, "user-1", "drive:a1:item-1", wait_s=5)   # notification, localization, language
    fake.language_service.resolve_ui_language.assert_awaited_once_with("user-1")
    assert fake.notification_service.notify_raw.await_args.args[:3] == ("user-1", "acc", "Deleted Drive/Inbox/a.txt")


def test_another_user_context_gets_its_own_instance():
    fake = _factory()
    assert UserAgentFactory._drive_operations(fake, _ctx()) is not UserAgentFactory._drive_operations(fake, _ctx())


def test_file_management_runs_on_the_users_shared_instance():
    fake, ctx = _factory(), _ctx()
    agent = UserAgentFactory._build_file_management(fake, "user-1", ctx)
    assert isinstance(agent, FileManagementAgent)
    assert agent.drive_operations is ctx.drive_operations is not None


def test_no_file_conversion_no_drive_operations():
    fake, ctx = _factory(file_conversion_service=None), _ctx()
    assert UserAgentFactory._drive_operations(fake, ctx) is None and ctx.drive_operations is None
