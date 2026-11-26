"""The Cabinet's memory-inbox settings routes (MCP_MEMORY_SUBMISSIONS_RFC §10.2): the triage frequency and the connected
apps with their revoke, over the real authorization service on the evaluating Firestore fake. Callers come from the
invented cast: usr-1 Alex (acc-1), usr-2 Clara (acc-2); the app is "Desk assistant"."""
from datetime import datetime, timezone
from typing import Any, List, Tuple
from unittest.mock import MagicMock

import pytest
from quart import Quart

from src.composition.connector_registry import ConnectorRegistry
from src.domain.mcp import MCPRefreshToken
from src.domain.user import UserProfile
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from tests.support.cabinet_session_kit import cabinet_session_for
from tests.unit.services.test_mcp_tasks_grant import FAR, World as AuthWorld
from tests.unit.services.test_mcp_resource_grants import INBOX, SCOPE_OF, TASKS

T0 = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
BASE = "/api/user/memory-inbox"
FIELD = "config.memory_inbox_triage_interval_days"


class LockedPin:
    async def facts_open(self, user_id: str, account_id: str, session_id: str) -> bool:
        return False


class Users:
    def __init__(self) -> None:
        self.user = UserProfile(user_id="usr-1", email="alex@example.com")
        self.writes: List[Tuple[Any, ...]] = []

    async def get_user(self, user_id: str):
        return self.user if user_id == "usr-1" else None

    async def update_user_fields(self, user: Any, fields: Tuple[str, ...]) -> None:
        self.writes.append(fields)


def token(n: int, grant_id: str, user: str = "usr-1", resource: str = INBOX, scopes=("memory_submit",),
          **over) -> MCPRefreshToken:
    return MCPRefreshToken(token_hash=f"{n:064d}", client_id="c1", user_id=user, account_id="acc-1",
                           scopes=list(scopes), resource=resource, expires_at=FAR, grant_id=grant_id, granted_at=T0,
                           issued_at=T0, **over)


class World:
    def __init__(self, facts_pin: Any = None, authorization: bool = True, user: str = "usr-1") -> None:
        self.auth = AuthWorld(scope_of=SCOPE_OF)
        self.users = Users()
        verifier, self.headers = cabinet_session_for(user, "acc-1")
        self.app = Quart("test_cabinet_memory_inbox_settings")
        self.app.register_blueprint(create_user_cabinet_blueprint(
            invite_service=MagicMock(), cabinet_sessions=verifier, user_repo=self.users, fact_repo=MagicMock(),
            embedding_service=MagicMock(), connectors=ConnectorRegistry(), facts_pin=facts_pin,
            mcp_authorization=self.auth.service if authorization else None))
        self.client = self.app.test_client()

    async def call(self, method: str, path: str, body: Any = None, headers: bool = True):
        kwargs: dict = {"headers": self.headers if headers else {}}
        if body is not None:
            kwargs["json"] = body
        return await self.client.open(f"{BASE}{path}", method=method, **kwargs)

    async def live(self, user: str = "usr-1") -> List[str]:
        got = await self.auth.repo.list_resource_grants(user, INBOX, T0)
        return sorted(t.grant_id for t in got)


async def test_settings_default_to_every_day_and_offer_the_four_choices():
    world = World()

    resp = await world.call("GET", "/settings")

    assert resp.status_code == 200
    assert await resp.get_json() == {"triage_interval_days": 1, "choices": [1, 2, 3, 7]}


@pytest.mark.parametrize("days", [1, 2, 3, 7])
async def test_each_choice_is_stored_on_its_field_and_read_back(days):
    world = World()

    resp = await world.call("PUT", "/settings", {"triage_interval_days": days})

    assert resp.status_code == 200 and (await resp.get_json())["triage_interval_days"] == days
    assert world.users.user.config.memory_inbox_triage_interval_days == days
    assert world.users.writes == [(FIELD,)]
    assert (await (await world.call("GET", "/settings")).get_json())["triage_interval_days"] == days


@pytest.mark.parametrize("body", [{"triage_interval_days": 0}, {"triage_interval_days": 5},
                                  {"triage_interval_days": "2"}, {"triage_interval_days": True}, {}, [1]])
async def test_any_other_value_is_400_and_writes_nothing(body):
    world = World()

    resp = await world.call("PUT", "/settings", body)

    assert resp.status_code == 400 and await resp.get_json() == {"error": "invalid_interval"}
    assert world.users.writes == [] and world.users.user.config.memory_inbox_triage_interval_days is None


async def test_the_apps_list_names_the_live_inbox_grants_of_the_caller_only():
    world = World()
    await world.auth.repo.save_refresh_token(token(1, "ga"))
    await world.auth.repo.save_refresh_token(token(2, "gb", user="usr-2"))
    await world.auth.repo.save_refresh_token(token(3, "gc", resource=TASKS, scopes=("external_tasks",), worker_id="w1"))

    resp = await world.call("GET", "/apps")

    assert resp.status_code == 200
    assert await resp.get_json() == {"apps": [{"grant_id": "ga", "client_name": "c1", "granted_at": T0.isoformat(),
                                               "last_refresh": T0.isoformat()}]}


async def test_revoking_a_listed_grant_ends_it_at_once():
    world = World()
    await world.auth.repo.save_refresh_token(token(1, "ga"))
    await world.auth.repo.save_refresh_token(token(2, "gb"))

    resp = await world.call("DELETE", "/apps/ga")

    assert resp.status_code == 200 and await resp.get_json() == {"revoked": True}
    assert await world.live() == ["gb"]
    assert await world.auth.service.grant_allowed("usr-1", "ga") is False


async def test_an_unknown_other_users_or_task_queue_grant_is_404_and_revokes_nothing():
    world = World()
    await world.auth.repo.save_refresh_token(token(1, "ga"))
    await world.auth.repo.save_refresh_token(token(2, "gb", user="usr-2"))
    await world.auth.repo.save_refresh_token(token(3, "gw", resource=TASKS, scopes=("external_tasks",), worker_id="w1"))

    for grant in ("nope", "gb", "gw"):
        resp = await world.call("DELETE", f"/apps/{grant}")
        assert resp.status_code == 404 and await resp.get_json() == {"error": "not_found"}

    assert await world.live() == ["ga"] and await world.live("usr-2") == ["gb"]
    assert await world.auth.service.grant_allowed("usr-1", "gw") is True


async def test_without_the_authorization_service_the_list_is_empty_and_a_revoke_is_404():
    world = World(authorization=False)

    assert await (await world.call("GET", "/apps")).get_json() == {"apps": []}
    assert (await world.call("DELETE", "/apps/ga")).status_code == 404


async def test_anonymous_callers_are_refused_on_all_four():
    world = World()
    calls = [("GET", "/settings", None), ("PUT", "/settings", {"triage_interval_days": 2}), ("GET", "/apps", None),
             ("DELETE", "/apps/ga", None)]

    for method, path, body in calls:
        resp = await world.call(method, path, body, headers=False)
        assert resp.status_code == 401
    assert world.users.writes == []
