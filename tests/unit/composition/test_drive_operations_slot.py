"""One DriveOperations per user (DRIVE_SPECIALIST_RFC §5.5): the factory builds it on first use, keeps it in the
user's context and hands the same instance to every agent that touches the drive, so the per-user mutation lock, the
shielded tasks and the late-failure notices are one set per user. A rebuilt user context (cache eviction, TTL) gets
a fresh one."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.agents.file_management_agent import FileManagementAgent
from src.composition.user_agent_factory import UserAgentFactory, _UserContext
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


def test_the_drive_operations_are_built_once_per_user_context_with_the_shared_services():
    fake, ctx = _factory(), _ctx()
    first = UserAgentFactory._drive_operations(fake, ctx)
    assert isinstance(first, DriveOperations) and ctx.drive_operations is first
    assert UserAgentFactory._drive_operations(fake, ctx) is first
    assert first.drive_service is fake.user_drive_service and first.drive_search is fake.drive_search_service
    assert first.notification is fake.notification_service and first.conversion_service is fake.file_conversion_service
    assert first.localization is fake.localization and first.language_service is fake.language_service


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
