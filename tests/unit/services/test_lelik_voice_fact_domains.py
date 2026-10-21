"""Lelik's call-start facts are an allowlist (decisions/lelik_warm_context.md, revised 2026-10-21)."""
from unittest.mock import patch

import pytest

from src.domain.entities import FactDomain
from src.domain.user import DEFAULT_VOICE_FACT_DOMAINS, UserBotConfig
from tests.unit.services.test_lelik_persona_service import _service


def test_default_allowlist_is_biographical_and_preference():
    assert UserBotConfig().voice_fact_domains == ["biographical", "preference"]
    assert set(DEFAULT_VOICE_FACT_DOMAINS) == {FactDomain.BIOGRAPHICAL.value, FactDomain.PREFERENCE.value}


def test_default_list_is_not_shared_between_configs():
    a, b = UserBotConfig(), UserBotConfig()
    a.voice_fact_domains.append("work")
    assert b.voice_fact_domains == ["biographical", "preference"]


def test_stored_doc_with_the_retired_key_still_loads():
    config = UserBotConfig(voice_excluded_fact_domains=["finance"])
    assert config.voice_fact_domains == ["biographical", "preference"]


@pytest.mark.asyncio
async def test_default_keeps_only_biographical_and_preference():
    service, *_ = _service()
    ctx = await service.assemble(user_id="u1", account_id="a1")
    domains = {f["domain"] for f in ctx.biographical_facts}
    assert domains == {"biographical", "preference"}
    assert "agent_directive" not in domains and "finance" not in domains


@pytest.mark.asyncio
async def test_per_user_override_is_honoured():
    service, *_ = _service(config=UserBotConfig(voice_fact_domains=["work"]))
    ctx = await service.assemble(user_id="u1", account_id="a1")
    assert {f["domain"] for f in ctx.biographical_facts} == {"work"}


@pytest.mark.asyncio
async def test_unknown_domain_warns_and_is_ignored():
    config = UserBotConfig(voice_fact_domains=["biographical", "biografical"])
    service, *_ = _service(config=config)
    with patch("src.services.lelik_persona_service.logger") as log:
        ctx = await service.assemble(user_id="u1", account_id="a1")
    assert {f["domain"] for f in ctx.biographical_facts} == {"biographical"}
    assert "biografical" in log.warning.call_args.args[0]
