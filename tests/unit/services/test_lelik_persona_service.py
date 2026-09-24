"""LelikPersonaService — Lelik's warm call-start context (decisions/lelik_warm_context.md)."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.entities import FactDomain
from src.domain.llm import Message, MessagePart, ToolCall
from src.domain.notification import NotificationChannel
from src.domain.session import SessionState
from src.domain.user import UserBotConfig
from src.ports.repository import FactRepository
from src.ports.session_store import SessionStore
from src.services.lelik_persona_service import (
    HISTORY_MESSAGES,
    MAX_ENTRY_CHARS,
    LelikPersonaService,
)

ALL_DOMAIN_FACTS = [{"domain": d.value, "text": f"fact: {d.value}"} for d in FactDomain]


def _channel(channel_id="C123"):
    return NotificationChannel(
        user_id="u1", platform="slack", channel_id=channel_id, updated_at=datetime.now(timezone.utc)
    )


def _service(config=None, facts=None, history=None, channel=...):
    fact_repo = AsyncMock(spec=FactRepository)
    fact_repo.get_biographical_context_cached.return_value = ALL_DOMAIN_FACTS if facts is None else facts
    session_store = AsyncMock(spec=SessionStore)
    session_store.load_session.return_value = SessionState(session_id="s", history=history or [])
    notifications = MagicMock()
    notifications.resolve_channel = AsyncMock(return_value=_channel() if channel is ... else channel)
    service = LelikPersonaService(
        fact_repository=fact_repo,
        session_store=session_store,
        notification_service=notifications,
        config=config or UserBotConfig(timezone="Europe/Madrid"),
    )
    return service, fact_repo, session_store, notifications


@pytest.mark.asyncio
async def test_every_fact_domain_reaches_the_prompt_by_default():
    service, fact_repo, *_ = _service()
    ctx = await service.assemble(user_id="u1", account_id="a1")
    fact_repo.get_biographical_context_cached.assert_awaited_once_with("a1")
    assert {f["domain"] for f in ctx.biographical_facts} == {d.value for d in FactDomain}


@pytest.mark.asyncio
async def test_excluded_domains_are_withheld():
    config = UserBotConfig(voice_excluded_fact_domains=["medical_records", "finance"])
    service, *_ = _service(config=config)
    ctx = await service.assemble(user_id="u1", account_id="a1")
    domains = {f["domain"] for f in ctx.biographical_facts}
    assert "medical_records" not in domains and "finance" not in domains
    assert FactDomain.WORK.value in domains


@pytest.mark.asyncio
async def test_unknown_excluded_domain_is_ignored_not_fatal():
    config = UserBotConfig(voice_excluded_fact_domains=["medcal_records"])
    service, *_ = _service(config=config)
    ctx = await service.assemble(user_id="u1", account_id="a1")
    assert len(ctx.biographical_facts) == len(ALL_DOMAIN_FACTS)


@pytest.mark.asyncio
async def test_history_is_read_from_the_channel_the_call_summary_is_written_to():
    service, _, session_store, notifications = _service(channel=_channel("D999"))
    await service.assemble(user_id="u1", account_id="a1")
    notifications.resolve_channel.assert_awaited_once_with("u1")
    session_store.load_session.assert_awaited_once_with("u1:D999")


@pytest.mark.asyncio
async def test_model_turns_use_the_stored_summary_not_full_text():
    history = [
        Message(role="user", parts=[MessagePart(text="what about the sea?")], created_at=1_700_000_000),
        Message(role="model", parts=[MessagePart(text="SUMMARY", full_text="LONG FULL ANSWER")], created_at=1_700_000_060),
    ]
    service, *_ = _service(history=history)
    ctx = await service.assemble(user_id="u1", account_id="a1")
    assert [e["content"] for e in ctx.conversation_history] == ["what about the sea?", "SUMMARY"]
    assert [e["role"] for e in ctx.conversation_history] == ["user", "alek"]


@pytest.mark.asyncio
async def test_history_is_capped_to_the_last_k_messages():
    history = [Message(role="user", parts=[MessagePart(text=f"m{i}")]) for i in range(HISTORY_MESSAGES + 10)]
    service, *_ = _service(history=history)
    ctx = await service.assemble(user_id="u1", account_id="a1")
    convo = ctx.conversation_history
    assert len(convo) == HISTORY_MESSAGES
    assert convo[0]["content"] == "m10" and convo[-1]["content"] == f"m{HISTORY_MESSAGES + 9}"


@pytest.mark.asyncio
async def test_long_entries_are_truncated():
    history = [Message(role="user", parts=[MessagePart(text="x" * (MAX_ENTRY_CHARS * 3))])]
    service, *_ = _service(history=history)
    ctx = await service.assemble(user_id="u1", account_id="a1")
    content = ctx.conversation_history[0]["content"]
    assert len(content) == MAX_ENTRY_CHARS + 1 and content.endswith("…")


@pytest.mark.asyncio
async def test_tool_parts_system_roles_and_empty_messages_are_skipped():
    history = [
        Message(role="model", parts=[MessagePart(tool_call=ToolCall(name="search_web", args={}))]),
        Message(role="system", parts=[MessagePart(text="internal")]),
        Message(role="user", parts=[MessagePart(file_data={"uri": "gs://x", "mime_type": "image/png"})]),
        Message(role="user", parts=[MessagePart(text="kept")]),
    ]
    service, *_ = _service(history=history)
    ctx = await service.assemble(user_id="u1", account_id="a1")
    assert [e["content"] for e in ctx.conversation_history] == ["kept"]


@pytest.mark.asyncio
async def test_timestamps_are_rendered_in_the_users_timezone():
    # 2023-11-14 22:13:20 UTC -> 23:13 in Europe/Madrid (CET, UTC+1)
    history = [Message(role="user", parts=[MessagePart(text="hi")], created_at=1_700_000_000)]
    service, *_ = _service(history=history)
    ctx = await service.assemble(user_id="u1", account_id="a1")
    assert ctx.conversation_history[0]["timestamp"] == "Nov 14, 23:13"


@pytest.mark.asyncio
async def test_no_resolvable_channel_means_no_history_not_a_failure():
    service, _, session_store, _ = _service(channel=None)
    ctx = await service.assemble(user_id="u1", account_id="a1")
    session_store.load_session.assert_not_awaited()
    assert ctx.conversation_history == []


@pytest.mark.asyncio
async def test_fact_store_failure_propagates_so_the_webhook_fails_closed():
    service, fact_repo, *_ = _service()
    fact_repo.get_biographical_context_cached.side_effect = RuntimeError("firestore down")
    with pytest.raises(RuntimeError):
        await service.assemble(user_id="u1", account_id="a1")


@pytest.mark.asyncio
async def test_primary_channel_is_the_notification_chain():
    service, _, _, notifications = _service(channel=_channel("D7"))
    channel = await service.primary_channel("u1")
    assert channel.channel_id == "D7"
    notifications.resolve_channel.assert_awaited_once_with("u1")


def test_user_bot_config_excludes_nothing_by_default():
    assert UserBotConfig().voice_excluded_fact_domains == []
