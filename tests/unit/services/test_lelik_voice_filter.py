"""Lelik's voice filter (FACT_PROFILE_RFC §6 row 4, §9 PR 11): a cached fact reaches the call only when its
`primary_domain` is in the account's voice list and its `sensitivity` is `ordinary`; absent is never ordinary. The default
list is the registry's `voice_open` set; a stored list equal to the old default reads as it, any other stored value maps
through the registry's legacy configuration map, and an unknown value is dropped with a warning.

The real service over the real repository on the evaluating Firestore fake: the cache is written with the repository's own
save and read back by the service; the session store and the notification chain only answer "no channel"."""
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.fact_domains import Domain, voice_open_default
from src.domain.user import UserBotConfig
from src.ports.session_store import SessionStore
from src.services.lelik_persona_service import LelikPersonaService
from tests.support.fact_kit import ACC
from tests.support.fact_store import FactStore

BORN = "Alex Doe was born on 1980-01-01 in Springfield"
PARTNER = "Alex's partner is Clara Moreira"
YOGA = "Alex does morning yoga for 30 minutes"
VEGETARIAN = "Alex is vegetarian"
DIABETES = "Alex was diagnosed with type 2 diabetes in 2022"
UNION = "Alex joined the Acme employees' trade union in November 2026, dues 12 EUR a month"
TENNIS = "Alex plays tennis on Sundays"


@pytest.fixture
def store(kit_db):
    return FactStore(kit_db=kit_db)


def _item(text, primary=None, sensitivity=None, domain="preference"):
    item = {"id": f"f-{text}", "text": text, "domain": domain, "tags": [], "context_priority": "medium",
            "created_at": "2026-11-01T00:00:00+00:00"}
    if primary is not None:
        item["primary_domain"] = primary
    if sensitivity is not None:
        item["sensitivity"] = sensitivity
    return item


CACHE = [
    _item(BORN, Domain.BIOGRAPHY, "ordinary", domain="biographical"),
    _item(YOGA, Domain.SPORT, "ordinary"),
    _item(VEGETARIAN, Domain.FOOD, "ordinary"),
    _item(DIABETES, Domain.HEALTH, "ordinary", domain="medical_records"),
    _item(UNION, Domain.WORK, "discreet", domain="work"),
    _item(TENNIS, Domain.SPORT, "ordinary"),
]


async def _voice_texts(store, config, cache=None):
    await store.repo.save_biographical_context_cache(ACC, {
        "facts": CACHE if cache is None else cache, "principles": [], "directives": []})
    notifications = MagicMock()
    notifications.resolve_channel = AsyncMock(return_value=None)
    service = LelikPersonaService(
        fact_repository=store.repo, session_store=AsyncMock(spec=SessionStore),
        notification_service=notifications, config=config,
    )
    context = await service.assemble(user_id="usr-1", account_id=ACC)
    return [f["text"] for f in context.biographical_facts]


class TestFilter:

    async def test_ordinary_facts_of_the_default_voice_domains_pass(self, store):
        assert await _voice_texts(store, UserBotConfig()) == [BORN, YOGA, VEGETARIAN, TENNIS]

    async def test_a_discreet_fact_of_a_listed_domain_is_dropped(self, store):
        config = UserBotConfig(voice_fact_domains=[Domain.WORK, Domain.SPORT])
        assert await _voice_texts(store, config) == [YOGA, TENNIS]

    async def test_an_ordinary_fact_outside_the_list_is_dropped(self, store):
        texts = await _voice_texts(store, UserBotConfig())
        assert DIABETES not in texts and Domain.HEALTH not in voice_open_default()

    @pytest.mark.parametrize("item", [
        _item(PARTNER),
        _item(PARTNER, primary=Domain.BIOGRAPHY),
        _item(PARTNER, sensitivity="ordinary"),
        _item(PARTNER, primary=Domain.BIOGRAPHY, sensitivity=""),
    ], ids=["no-profile", "no-sensitivity", "no-primary-domain", "empty-sensitivity"])
    async def test_a_fact_without_its_profile_fields_never_passes(self, store, item):
        assert await _voice_texts(store, UserBotConfig(), cache=[item, _item(YOGA, Domain.SPORT, "ordinary")]) == [YOGA]

    async def test_an_intimate_fact_of_a_listed_domain_is_dropped(self, store):
        cache = [_item("fact: sport", Domain.SPORT, "intimate"), _item(TENNIS, Domain.SPORT, "ordinary")]
        assert await _voice_texts(store, UserBotConfig(), cache=cache) == [TENNIS]

    async def test_a_cache_written_before_the_profile_fields_gives_no_facts(self, store):
        cache = [{"id": "f-1", "text": BORN, "domain": "biographical", "tags": []}]
        assert await _voice_texts(store, UserBotConfig(), cache=cache) == []


class TestStoredList:

    def test_the_default_list_is_the_registry_voice_open_set(self):
        assert UserBotConfig().voice_domain_keys() == (voice_open_default(), ())

    @pytest.mark.parametrize("stored", [["biographical", "preference"], ["preference", "biographical"]])
    def test_the_old_default_in_any_order_reads_as_the_new_default(self, stored):
        assert UserBotConfig(voice_fact_domains=stored).voice_domain_keys() == (voice_open_default(), ())

    async def test_the_old_default_lets_the_new_default_domains_through(self, store):
        config = UserBotConfig(voice_fact_domains=["preference", "biographical"])
        assert await _voice_texts(store, config) == [BORN, YOGA, VEGETARIAN, TENNIS]

    def test_an_old_non_default_value_maps_to_its_registry_keys(self):
        config = UserBotConfig(voice_fact_domains=["medical_records", "finance"])
        assert config.voice_domain_keys() == ((Domain.HEALTH, Domain.MONEY, Domain.SAVINGS), ())

    async def test_an_old_non_default_value_lets_its_mapped_domains_through(self, store):
        config = UserBotConfig(voice_fact_domains=["medical_records"])
        assert await _voice_texts(store, config) == [DIABETES]

    def test_registry_keys_and_old_values_mix_without_duplicates(self):
        config = UserBotConfig(voice_fact_domains=[Domain.HEALTH, "medical_records", Domain.WORK])
        assert config.voice_domain_keys() == ((Domain.HEALTH, Domain.WORK), ())

    async def test_an_unknown_value_is_dropped_with_the_warning(self, store, caplog):
        config = UserBotConfig(voice_fact_domains=["sportt", Domain.SPORT])
        with caplog.at_level(logging.WARNING):
            texts = await _voice_texts(store, config)
        assert texts == [YOGA, TENNIS]
        assert config.voice_domain_keys() == ((Domain.SPORT,), ("sportt",))
        assert any("[LelikPersona] Ignoring unknown voice_fact_domains" in r.getMessage() and "sportt" in r.getMessage()
                   for r in caplog.records)

    async def test_no_warning_when_every_value_is_known(self, store, caplog):
        with caplog.at_level(logging.WARNING):
            await _voice_texts(store, UserBotConfig(voice_fact_domains=["work"]))
        assert not any("Ignoring unknown voice_fact_domains" in r.getMessage() for r in caplog.records)
