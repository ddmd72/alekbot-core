"""LanguageResolver (the read side) and LanguagePreferenceService (the write side) over the real user, account and
prompt-override repositories on one `EvalDb`: what the writer stores is what the resolver reads back, and the writer's
fallback language is the resolver's system default."""
from types import SimpleNamespace
from typing import Any, Dict, List

from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.adapters.prompt_v3.firestore_agent_profile_repository import FirestoreAgentProfileRepository
from src.domain.billing import BillingAccount
from src.domain.language import LanguageCode
from src.domain.user import UserProfile
from src.ports.language_service_port import LanguageServicePort
from src.services.language_preference_service import _LANG_ALERTS, LanguagePreferenceService
from src.services.language_resolver import LanguageResolver
from tests.support.firestore_eval import EvalDb

ENV = SimpleNamespace(domain_users_collection="users", is_production=False)


class Notifier:
    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []

    async def notify(self, **kwargs: Any) -> None:
        self.sent.append(kwargs)


class World:
    def __init__(self, system_default: LanguageCode = LanguageCode.EN) -> None:
        self.db = EvalDb()
        self.accounts = FirestoreAccountRepository(self.db, "accounts")
        self.users = FirestoreUserRepository(self.db, ENV, self.accounts)
        self.resolver = LanguageResolver(self.users, self.accounts, system_default_language=system_default)
        self.notifier = Notifier()
        self.refreshed: List[str] = []
        self.invalidations = 0

        async def refresh(user_id: str) -> None:
            self.refreshed.append(user_id)

        def invalidate() -> None:
            self.invalidations += 1

        self.writer = LanguagePreferenceService(
            user_repo=self.users,
            profile_repo=FirestoreAgentProfileRepository(self.db, "profiles", "overrides"),
            prompt_builder=SimpleNamespace(invalidate_cache=invalidate),
            resolver=self.resolver,
            notification_service=self.notifier,
            ensure_agents=refresh,
        )

    async def seed(self, *, account_language=None, account_id="acct-1") -> None:
        if account_id:
            await self.accounts.create_account(BillingAccount(account_id=account_id, default_language=account_language))
        await self.users.create_user(UserProfile(user_id="user-1", account_id=account_id))

    def override_tokens(self) -> set:
        doc = self.db.collection("overrides").docs.get("USER_user-1") or {}
        return set(doc.get("tokens") or {})


def test_the_resolver_is_the_language_service_port():
    assert isinstance(World().resolver, LanguageServicePort)


def test_the_resolver_exposes_its_system_default():
    assert World(LanguageCode.FR).resolver.system_default == LanguageCode.FR


async def test_an_unknown_user_reads_the_defaults():
    world = World(LanguageCode.ES)
    assert await world.resolver.get_preference("nobody") == (None, True)
    assert await world.resolver.resolve_ui_language("nobody") == LanguageCode.ES


async def test_the_account_language_applies_until_the_user_chooses_one():
    world = World()
    await world.seed(account_language=LanguageCode.FR)
    assert await world.resolver.resolve_ui_language("user-1") == LanguageCode.FR

    await world.writer.set_preference("user-1", LanguageCode.UK, agent_mirror=False)

    assert await world.resolver.resolve_ui_language("user-1") == LanguageCode.UK
    assert await world.resolver.get_preference("user-1") == (LanguageCode.UK, False)


async def test_a_user_without_an_account_falls_to_the_system_default():
    world = World(LanguageCode.FR)
    await world.seed(account_id=None)
    assert await world.resolver.resolve_ui_language("user-1") == LanguageCode.FR


async def test_a_fixed_language_without_a_choice_writes_the_resolvers_default():
    world = World(LanguageCode.FR)
    await world.seed()

    await world.writer.set_preference("user-1", None, agent_mirror=False)

    assert world.override_tokens() == {"LANG_FIXED_FR"}
    assert [n["system_alert"] for n in world.notifier.sent] == [_LANG_ALERTS["fr"]]


async def test_a_change_refreshes_the_users_agents_then_notifies_and_invalidates_the_prompt_cache():
    world = World()
    await world.seed()

    await world.writer.set_preference("user-1", None, agent_mirror=True)

    assert world.refreshed == ["user-1"]
    assert [(n["user_id"], n["account_id"], n["system_alert"]) for n in world.notifier.sent] == [
        ("user-1", "acct-1", _LANG_ALERTS["mirror"])]
    assert world.invalidations == 1
    assert world.override_tokens() == set()
    assert await world.resolver.get_preference("user-1") == (None, True)
