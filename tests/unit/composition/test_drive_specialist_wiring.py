"""The drive specialist in the factory (DRIVE_SPECIALIST_RFC §5.1, §5.5): a lazy agent built on the user's shared
DriveOperations — the same instance FileManagement holds — with the user's timezone, indexing switch and thinking
(`agent_thinking["drive"]`, else medium). No file conversion, no specialist. Its prompt is warmed with Smart's."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.agents.drive_agent import DriveAgent
from src.composition.drive_specialist_build import build_drive_specialist
from src.composition.user_agent_factory import UserAgentFactory, _UserContext
from src.domain.user import UserBotConfig
from src.infrastructure.agent_config import DRIVE_SPECIALIST as DRIVE_CFG


def _ctx(config: UserBotConfig) -> _UserContext:
    profile = MagicMock()
    profile.config = config
    return _UserContext(user_profile=profile, prompt_builder=MagicMock(name="prompt_builder"))


def _factory(**over) -> SimpleNamespace:
    context_builder = MagicMock()
    context_builder.build.return_value = MagicMock(provider=MagicMock(), model_name="claude-sonnet-5-5")
    fields = dict(context_builder=context_builder, file_conversion_service=MagicMock(), file_storage=MagicMock(),
                  notification_service=MagicMock(), user_drive_service=MagicMock(), localization=MagicMock(),
                  language_service=MagicMock(), drive_search_service=MagicMock(), notes_provider=None)
    fields.update(over)
    fake = SimpleNamespace(**fields)
    fake._drive_operations = lambda ctx: UserAgentFactory._drive_operations(fake, ctx)
    return fake


def test_the_specialist_and_file_management_share_the_users_drive_operations():
    fake, ctx = _factory(), _ctx(UserBotConfig(timezone="Europe/Madrid"))
    drive = UserAgentFactory._build_drive(fake, "user-1", ctx)
    files = UserAgentFactory._build_file_management(fake, "user-1", ctx)
    assert isinstance(drive, DriveAgent)
    assert drive.operations is files.drive_operations is ctx.drive_operations is not None


def test_the_agent_is_built_from_the_users_config():
    config = UserBotConfig(timezone="Europe/Madrid", drive_indexing_enabled=True)
    fake, ctx = _factory(), _ctx(config)
    agent = UserAgentFactory._build_drive(fake, "user-1", ctx)
    assert (agent.agent_id, agent.agent_type) == ("drive_agent_user-1", "drive")
    assert agent.config.timeout_ms == DRIVE_CFG.timeout_ms == 180_000
    fake.context_builder.build.assert_called_once_with("drive", config)
    assert agent.prompt_builder is ctx.prompt_builder and agent.drive_search is fake.drive_search_service
    assert agent.user_timezone == "Europe/Madrid" and agent.indexing_enabled is True
    assert agent.thinking_effort == "medium"


def test_a_users_agent_thinking_wins_over_the_default():
    agent = build_drive_specialist("u", context_builder=_factory().context_builder, prompt_builder=MagicMock(),
                                   user_config=UserBotConfig(agent_thinking={"drive": "low"}), operations=MagicMock(),
                                   drive_search=None)
    assert agent.thinking_effort == "low"


def test_no_file_conversion_no_specialist():
    fake = _factory(file_conversion_service=None)
    assert UserAgentFactory._build_drive(fake, "user-1", _ctx(UserBotConfig())) is None


def test_lazy_dispatch_tables_include_the_specialist():
    assert UserAgentFactory._LAZY_BUILDERS["drive"] is UserAgentFactory._build_drive
    assert UserAgentFactory._LAZY_AGENT_IDS["drive"] == "drive_agent"


def test_the_drive_prompt_is_warmed_where_a_drive_path_exists():
    assert UserAgentFactory._preload_agent_types(_factory()) == ["quick", "smart", "drive"]
    assert UserAgentFactory._preload_agent_types(_factory(notes_provider=MagicMock())) == [
        "quick", "smart", "notes", "drive"]
    assert UserAgentFactory._preload_agent_types(_factory(file_conversion_service=None)) == ["quick", "smart"]
