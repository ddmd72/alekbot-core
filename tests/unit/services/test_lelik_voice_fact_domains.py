"""Lelik's call-start facts are an allowlist (decisions/lelik_warm_context.md, revised 2026-10-21).

Reviewer edit (FACT_PROFILE_RFC §6 row 4, PR 11): the allowlist holds registry keys matched on the cached facts'
primary_domain; the default is the registry's voice_open set (VOICE_OPEN, written out in the persona test)."""
from unittest.mock import patch

import pytest

from src.domain.user import DEFAULT_VOICE_FACT_DOMAINS, UserBotConfig
from tests.unit.services.test_lelik_persona_service import VOICE_OPEN, _service


def test_default_allowlist_is_the_voice_open_set():
    assert UserBotConfig().voice_fact_domains == list(DEFAULT_VOICE_FACT_DOMAINS)
    assert set(DEFAULT_VOICE_FACT_DOMAINS) == VOICE_OPEN and len(DEFAULT_VOICE_FACT_DOMAINS) == len(VOICE_OPEN)


def test_default_list_is_not_shared_between_configs():
    a, b = UserBotConfig(), UserBotConfig()
    a.voice_fact_domains.append("work")
    assert b.voice_fact_domains == list(DEFAULT_VOICE_FACT_DOMAINS) and "work" not in b.voice_fact_domains


def test_stored_doc_with_the_retired_key_still_loads():
    config = UserBotConfig(voice_excluded_fact_domains=["finance"])
    assert config.voice_fact_domains == list(DEFAULT_VOICE_FACT_DOMAINS)


@pytest.mark.asyncio
async def test_default_keeps_only_the_voice_open_domains():
    service, *_ = _service()
    ctx = await service.assemble(user_id="u1", account_id="a1")
    domains = {f["primary_domain"] for f in ctx.biographical_facts}
    assert domains == VOICE_OPEN
    assert "agent_directives" not in domains and "money" not in domains


@pytest.mark.asyncio
async def test_per_user_override_is_honoured():
    service, *_ = _service(config=UserBotConfig(voice_fact_domains=["work"]))
    ctx = await service.assemble(user_id="u1", account_id="a1")
    assert {f["primary_domain"] for f in ctx.biographical_facts} == {"work"}


@pytest.mark.asyncio
async def test_unknown_domain_warns_and_is_ignored():
    # The known value is an old FactDomain value: it maps to its registry key (biography) while the typo is dropped.
    config = UserBotConfig(voice_fact_domains=["biographical", "biografical"])
    service, *_ = _service(config=config)
    with patch("src.services.lelik_persona_service.logger") as log:
        ctx = await service.assemble(user_id="u1", account_id="a1")
    assert {f["primary_domain"] for f in ctx.biographical_facts} == {"biography"}
    assert "biografical" in log.warning.call_args.args[0]
