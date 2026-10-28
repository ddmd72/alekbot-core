"""The drive specialist in the factory (DRIVE_SPECIALIST_RFC §5.1, §5.5): a lazy agent built on the user's shared
DriveOperations — the same instance FileManagement holds — with the user's prompt builder, timezone and thinking
(`agent_thinking["drive"]`, else medium), and the ops alert sink the consolidation agent gets. No file conversion, no
specialist. Its prompt is warmed with Smart's.

DriveAgent keeps its collaborators private: the prompt builder, the timezone and the sink are observed through a run
with a scripted LLM (tests/support/scripted_llm.py)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.agents.drive_agent import DriveAgent
from src.composition.drive_specialist_build import build_drive_specialist
from src.composition.user_agent_factory import UserAgentFactory, _UserContext
from src.domain import drive_tool_texts as tt
from src.domain.agent import AgentIntent, AgentMessage
from src.domain.drive_line import DriveLine
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_preflight import DrivePreflight
from src.domain.drive_search_line import SearchLine
from src.domain.llm import PROMPT_CACHE_BOUNDARY
from src.domain.user import UserBotConfig
from src.infrastructure.agent_config import DRIVE
from src.infrastructure.drive_operations import DriveOperations
from tests.support.scripted_llm import calls, script


def _ctx(config: UserBotConfig) -> _UserContext:
    profile = MagicMock()
    profile.config = config
    prompts = AsyncMock(name="prompt_builder")
    prompts.build_for_agent.return_value = f"DriveAgent extends Agent {{}}\n\n{PROMPT_CACHE_BOUNDARY}"
    return _UserContext(user_profile=profile, prompt_builder=prompts)


def _factory(**over) -> SimpleNamespace:
    context_builder = MagicMock()
    context_builder.build.return_value = MagicMock(provider=MagicMock(), model_name="claude-sonnet-5-5")
    fields = dict(context_builder=context_builder, file_conversion_service=MagicMock(), file_storage=MagicMock(),
                  notification_service=MagicMock(), user_drive_service=MagicMock(), localization=MagicMock(),
                  language_service=MagicMock(), drive_search_service=MagicMock(), notes_provider=None,
                  alert_sink=AsyncMock(name="ops_alert_sink"))
    fields.update(over)
    fake = SimpleNamespace(**fields)
    fake._drive_operations = lambda ctx: UserAgentFactory._drive_operations(fake, ctx)
    return fake


def _scripted_ops() -> AsyncMock:
    ops = AsyncMock(spec=DriveOperations)
    ops.preflight.return_value = DrivePreflight(DriveLine.CONNECTED, SearchLine.ON)
    ops.list_folder.return_value = DriveOutcome(op=DriveOp.LIST, cls=OutcomeClass.OK, text="(empty)", success=True)
    return ops


def _msg() -> AgentMessage:
    return AgentMessage.create(sender="coordinator", recipient="drive_agent_user-1", intent=AgentIntent.QUERY,
                               payload={"query": "list my drive", "intent": "work_with_drive_files"},
                               context={"user_id": "user-1", "account_id": "acc-1", "interactive_delivery": True})


def test_the_specialist_and_file_management_share_the_users_drive_operations():
    fake, ctx = _factory(), _ctx(UserBotConfig(timezone="Europe/Madrid"))
    drive = UserAgentFactory._build_drive(fake, "user-1", ctx)
    files = UserAgentFactory._build_file_management(fake, "user-1", ctx)
    assert isinstance(drive, DriveAgent)
    assert drive.operations is files.drive_operations is ctx.drive_operations is not None


async def test_the_agent_is_built_from_the_users_config():
    config = UserBotConfig(timezone="Europe/Madrid")
    fake, ctx = _factory(), _ctx(config)
    ctx.drive_operations = _scripted_ops()
    agent = UserAgentFactory._build_drive(fake, "user-1", ctx)
    assert (agent.agent_id, agent.agent_type) == ("drive_agent_user-1", "drive")
    assert agent.config.timeout_ms == DRIVE.timeout_ms == 180_000
    fake.context_builder.build.assert_called_once_with("drive", config)
    assert agent.thinking == "medium"
    agent._call_llm, seen = script(calls((tt.REPORT_TOOL, {"note": "Listed nothing."})))
    await agent.execute(_msg())
    ctx.prompt_builder.build_for_agent.assert_awaited_once()
    assert ctx.prompt_builder.build_for_agent.await_args.kwargs["agent_type"] == "drive"
    assert "Europe/Madrid" in seen[0].system_instruction        # the user's timezone in the state block
    assert seen[0].thinking == "medium"


def test_a_users_agent_thinking_wins_over_the_default():
    agent = build_drive_specialist("u", context_builder=_factory().context_builder, prompt_builder=MagicMock(),
                                   user_config=UserBotConfig(agent_thinking={"drive": "low"}), operations=MagicMock(),
                                   alert_sink=None)
    assert agent.thinking == "low"


async def test_a_turn_cap_exit_alerts_the_ops_sink_the_factory_holds():
    """The ops AlertSinkPort the consolidation agent gets (UserAgentFactory.alert_sink) reaches the specialist."""
    fake, ctx = _factory(), _ctx(UserBotConfig())
    ctx.drive_operations = _scripted_ops()
    agent = UserAgentFactory._build_drive(fake, "user-1", ctx)
    agent._call_llm, _ = script(*[calls((tt.LIST_FOLDER, {"folder": f"F{i}"})) for i in range(DRIVE.max_turns)])
    await agent.execute(_msg())
    fake.alert_sink.post.assert_awaited_once()
    assert fake.alert_sink.post.await_args.args[0].startswith("🚨 Drive specialist hit the turn limit")


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
