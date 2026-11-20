"""`GET/POST /api/user/language` in the Cabinet: the read goes through the `LanguageResolver`, the write through the
`LanguagePreferenceService` (the one write path), both over the real user, account and prompt-override repositories on
one `EvalDb`. Assertions are on the stored documents and on what the read route returns."""
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

from quart import Quart

from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.adapters.prompt_v3.firestore_agent_profile_repository import FirestoreAgentProfileRepository
from src.domain.billing import BillingAccount
from src.domain.user import UserProfile
from src.services.language_preference_service import LanguagePreferenceService
from src.services.language_resolver import LanguageResolver
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from tests.support.firestore_eval import EvalDb

TOKEN = {"Authorization": "Bearer t"}
ENV = SimpleNamespace(domain_users_collection="users", is_production=False)


class Notifier:
    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []

    async def notify(self, **kwargs: Any) -> None:
        self.sent.append(kwargs)


class World:
    def __init__(self) -> None:
        self.db = EvalDb()
        accounts = FirestoreAccountRepository(self.db, "accounts")
        self.accounts = accounts
        self.users = FirestoreUserRepository(self.db, ENV, accounts)
        self.resolver = LanguageResolver(self.users, accounts)
        self.notifier = Notifier()
        self.writer = LanguagePreferenceService(
            user_repo=self.users,
            profile_repo=FirestoreAgentProfileRepository(self.db, "profiles", "overrides"),
            prompt_builder=SimpleNamespace(invalidate_cache=lambda: None),
            resolver=self.resolver,
            notification_service=self.notifier,
        )

    async def seed(self) -> None:
        await self.accounts.create_account(BillingAccount(account_id="acct-1"))
        await self.users.create_user(UserProfile(user_id="user-1", account_id="acct-1"))

    def stored_config(self) -> Dict[str, Any]:
        return self.db.collection("users").docs["user-1"]["config"]

    def app(self, *, reader: Optional[LanguageResolver], writer: Optional[LanguagePreferenceService]) -> Quart:
        sessions = MagicMock()
        sessions.verify_access_token = MagicMock(return_value={"sub": "user-1", "account_id": "acct-1", "role": "owner"})
        app = Quart("test_cabinet_language_routes")
        app.register_blueprint(create_user_cabinet_blueprint(
            invite_service=MagicMock(), session_service=sessions, user_repo=self.users, fact_repo=MagicMock(),
            embedding_service=MagicMock(), language_service=reader, language_preferences=writer,
        ))
        return app


async def test_a_saved_choice_is_stored_by_the_writer_and_read_back_by_the_resolver():
    world = World()
    await world.seed()
    app = world.app(reader=world.resolver, writer=world.writer)

    async with app.test_client() as client:
        posted = await client.post("/api/user/language", json={"preferred_language": "fr", "agent_mirror": False},
                                   headers=TOKEN)
        got = await client.get("/api/user/language", headers=TOKEN)

    assert posted.status_code == 200
    assert world.stored_config()["preferred_language"] == "fr" and world.stored_config()["agent_mirror"] is False
    assert "LANG_FIXED_FR" in world.db.collection("overrides").docs["USER_user-1"]["tokens"]
    assert len(world.notifier.sent) == 1
    assert got.status_code == 200
    assert await got.get_json() == {"preferred_language": "fr", "agent_mirror": False}


async def test_without_a_writer_a_save_stores_nothing():
    world = World()
    await world.seed()
    app = world.app(reader=world.resolver, writer=None)

    async with app.test_client() as client:
        posted = await client.post("/api/user/language", json={"preferred_language": "fr", "agent_mirror": False},
                                   headers=TOKEN)

    assert posted.status_code == 200
    assert world.stored_config().get("preferred_language") is None
    assert world.notifier.sent == []


async def test_without_a_resolver_the_read_answers_the_defaults():
    world = World()
    await world.seed()
    await world.writer.set_preference("user-1", None, agent_mirror=False)
    app = world.app(reader=None, writer=world.writer)

    async with app.test_client() as client:
        got = await client.get("/api/user/language", headers=TOKEN)

    assert await got.get_json() == {"preferred_language": None, "agent_mirror": True}


async def test_an_unsupported_code_is_400_and_stores_nothing():
    world = World()
    await world.seed()
    app = world.app(reader=world.resolver, writer=world.writer)

    async with app.test_client() as client:
        posted = await client.post("/api/user/language", json={"preferred_language": "xx", "agent_mirror": False},
                                   headers=TOKEN)

    assert posted.status_code == 400
    assert world.stored_config().get("preferred_language") is None
    assert world.notifier.sent == []
    assert "USER_user-1" not in world.db.collection("overrides").docs


async def test_a_non_string_code_is_400():
    world = World()
    await world.seed()
    app = world.app(reader=world.resolver, writer=world.writer)

    async with app.test_client() as client:
        posted = await client.post("/api/user/language", json={"preferred_language": 5, "agent_mirror": False},
                                   headers=TOKEN)

    assert posted.status_code == 400
    assert world.stored_config().get("preferred_language") is None
    assert world.notifier.sent == []


async def test_an_upper_case_code_is_accepted_as_lower_case():
    world = World()
    await world.seed()
    app = world.app(reader=world.resolver, writer=world.writer)

    async with app.test_client() as client:
        posted = await client.post("/api/user/language", json={"preferred_language": "FR", "agent_mirror": False},
                                   headers=TOKEN)

    assert posted.status_code == 200
    assert world.stored_config()["preferred_language"] == "fr"


async def test_a_null_code_clears_the_choice():
    world = World()
    await world.seed()
    app = world.app(reader=world.resolver, writer=world.writer)

    async with app.test_client() as client:
        await client.post("/api/user/language", json={"preferred_language": "fr", "agent_mirror": False},
                          headers=TOKEN)
        posted = await client.post("/api/user/language", json={"preferred_language": None, "agent_mirror": True},
                                   headers=TOKEN)

    assert posted.status_code == 200
    assert world.stored_config().get("preferred_language") is None
