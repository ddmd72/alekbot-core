"""One DriveOperations per user (DRIVE_SPECIALIST_RFC §5.5): the factory builds it on first use, keeps it in the
user's context and hands the same instance to every agent that touches the drive, so the per-user mutation lock, the
shielded tasks and the late-failure notices are one set per user. A rebuilt user context (cache eviction, TTL) gets
a fresh one."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.agents.file_management_agent import FileManagementAgent
from src.composition.user_agent_factory import UserAgentFactory, _UserContext
from src.domain.drive_search_line import SearchLine
from src.domain.language import LanguageCode
from src.infrastructure.drive_operations import DriveOperations


def _ctx() -> _UserContext:
    profile = MagicMock()
    profile.config.timezone = "Europe/Madrid"
    return _UserContext(user_profile=profile, prompt_builder=MagicMock())


def _factory(**over) -> SimpleNamespace:
    fields = dict(file_conversion_service=MagicMock(), file_storage=MagicMock(), notification_service=MagicMock(),
                  user_drive_service=MagicMock(), localization=MagicMock(), language_service=MagicMock(),
                  drive_search_service=MagicMock())
    fields.update(over)
    fake = SimpleNamespace(**fields)
    fake._drive_operations = lambda ctx: UserAgentFactory._drive_operations(fake, ctx)
    return fake


def test_the_drive_operations_are_built_once_per_user_context():
    fake, ctx = _factory(), _ctx()
    first = UserAgentFactory._drive_operations(fake, ctx)
    assert isinstance(first, DriveOperations) and ctx.drive_operations is first
    assert UserAgentFactory._drive_operations(fake, ctx) is first


async def test_every_shared_service_reaches_the_instance():
    """DriveOperations keeps its services private: each one is observed through the call it makes."""
    fake, ctx = _factory(), _ctx()
    fake.user_drive_service.connection = AsyncMock(side_effect=RuntimeError("offline"))
    fake.user_drive_service.delete = AsyncMock(return_value=SimpleNamespace(
        item=SimpleNamespace(is_folder=False, path="Inbox/a.txt")))
    fake.drive_search_service.search_state = AsyncMock(return_value=SearchLine.REBUILDING)
    fake.file_conversion_service.resolve_content = AsyncMock(return_value="chat file text")
    fake.language_service.resolve_ui_language = AsyncMock(return_value=LanguageCode.EN)
    fake.localization.get_ui_string = MagicMock(return_value="Deleted {path}")
    fake.notification_service.notify_raw = AsyncMock()
    ops = UserAgentFactory._drive_operations(fake, ctx)

    preflight = await ops.preflight("user-1")                       # the drive service and the drive search
    assert preflight.search is SearchLine.REBUILDING
    fake.user_drive_service.connection.assert_awaited_once_with("user-1")
    opened = await ops.fetch_stored({}, "user-1", "notes.txt")      # the conversion service
    assert opened.text == "chat file text"
    await ops.delete({"account_id": "acc"}, "user-1", "drive:item-1", wait_s=5)   # notification, localization, language
    fake.language_service.resolve_ui_language.assert_awaited_once_with("user-1")
    assert fake.notification_service.notify_raw.await_args.args[:3] == ("user-1", "acc", "Deleted Inbox/a.txt")


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
