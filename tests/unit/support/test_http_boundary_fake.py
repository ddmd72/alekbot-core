"""The routed aiohttp fake (P15 card B) under the real To Do, Gmail and Slack webhook adapters."""
from datetime import datetime, timedelta, timezone

import aiohttp
import pytest

from src.adapters.gmail_provider_adapter import GmailProviderAdapter
from src.adapters.microsoft.todo_adapter import MicrosoftToDoAdapter
from src.adapters.slack.webhook_adapter import SlackWebhookAdapter
from src.domain.integration import Integration
from src.domain.oauth_credential import OAuthCredential
from src.domain.provider_account import ProviderAccount
from src.domain.task import TaskUserConfig
from tests.support import http_boundary_fake as hbf
from tests.support.oauth_credentials_store import creds
from src.composition.connector_registry import ConnectorRegistry

_GRAPH = "https://graph.microsoft.com/v1.0"
_HOOK = "https://hooks.example.com/services/T1/B1/x"


class _Creds:
    async def list_for_user(self, user_id, kind=None, *a, **k):
        return [await self.get(user_id, "")]

    async def get(self, user_id, credential_id=None, *a, **k):
        return OAuthCredential(
            user_id=user_id,
            account_id="acct-1",
            kind="microsoft_todo",
            integration=Integration.TASKS,
            account=ProviderAccount(realm="microsoft", subject="", label="u1@example.com"),
            access_token="tok",
            refresh_token="ref",
            token_expiry=datetime.now(timezone.utc) + timedelta(hours=1),
            scopes=["Tasks.ReadWrite"],
        )

    async def save(self, *a, **k):
        return None

    async def update(self, *a, **k):
        return True


class _TaskConfig:
    async def get_config(self, *a, **k):
        return TaskUserConfig()

    async def save_config(self, *a, **k):
        return None

    async def set_primary_list_id_if_absent(self, *a, **k):
        return "l1"


def _todo() -> MicrosoftToDoAdapter:
    return MicrosoftToDoAdapter(oauth_credentials=_Creds(), task_config=_TaskConfig(),
                                client_id="cid", client_secret="sec", connector=ConnectorRegistry().get("microsoft_todo"))


async def test_ensure_primary_list_creates_the_list_with_one_post(monkeypatch):
    fake = hbf.install(monkeypatch)
    fake.todo_empty_account()
    assert await _todo().ensure_primary_list("u1") == "l1"
    posts = fake.requests_to("POST", f"{_GRAPH}/me/todo/lists")
    assert len(posts) == 1 and posts[0].json == {"displayName": "Alek Bot Tasks"}
    assert fake.unrouted == []


async def test_listing_shows_the_list_only_after_the_post(monkeypatch):
    fake = hbf.install(monkeypatch)
    fake.todo_empty_account()
    adapter = _todo()
    assert await adapter.list_task_lists("u1") == []
    await adapter.ensure_primary_list("u1")
    assert [lst.list_id for lst in await adapter.list_task_lists("u1")] == ["l1"]


async def test_register_subscription_posts_once(monkeypatch):
    fake = hbf.install(monkeypatch)
    fake.todo_empty_account(sub_id="s9")
    cfg = await _todo().register_subscription("u1", "l1", "https://app.example.com")
    assert cfg.sub_id == "s9"
    subs = fake.requests_to("POST", f"{_GRAPH}/subscriptions")
    assert len(subs) == 1 and subs[0].json["resource"] == "/me/todo/lists/l1/tasks"


async def test_list_tasks_reads_the_empty_list(monkeypatch):
    fake = hbf.install(monkeypatch)
    fake.todo_empty_account()
    assert await _todo().list_tasks("u1", list_id="l1") == []


async def test_gmail_list_emails_over_empty_mailbox(monkeypatch):
    fake = hbf.install(monkeypatch)
    fake.gmail_empty()
    adapter = GmailProviderAdapter(client_id="cid", client_secret="sec")
    assert await adapter.list_emails(creds("u1", "gmail"), max_results=10) == ([], None)
    (req,) = fake.requests
    assert req.method == "GET" and req.params == {"maxResults": 10}


async def test_slack_webhook_post_lands_in_posts(monkeypatch):
    fake = hbf.install(monkeypatch)
    fake.slack_webhook(_HOOK)
    await SlackWebhookAdapter(_HOOK).post("x")
    assert fake.posts == [{"text": "x"}]


async def test_slack_webhook_refusal_raises(monkeypatch):
    fake = hbf.install(monkeypatch)
    fake.route("POST", _HOOK, 500, "boom")
    with pytest.raises(RuntimeError, match="500"):
        await SlackWebhookAdapter(_HOOK).post("x")


async def test_unrouted_get_raises_and_is_recorded(monkeypatch):
    fake = hbf.install(monkeypatch)
    async with aiohttp.ClientSession() as session:
        with pytest.raises(aiohttp.ClientConnectionError, match="no route in the fake: GET https://nowhere.example.com/a"):
            async with session.get("https://nowhere.example.com/a"):
                pass
    assert [(r.method, r.url) for r in fake.unrouted] == [("GET", "https://nowhere.example.com/a")]


async def test_longest_prefix_wins(monkeypatch):
    fake = hbf.install(monkeypatch)
    fake.route("GET", "https://a.example.com/", body={"w": "short"}, prefix=True)
    fake.route("GET", "https://a.example.com/x/", body={"w": "long"}, prefix=True)
    async with aiohttp.ClientSession() as s:
        async with s.get("https://a.example.com/x/1") as r:
            assert await r.json() == {"w": "long"}
        async with s.get("https://a.example.com/y") as r:
            assert await r.json() == {"w": "short"}


async def test_handler_sees_request_and_requests_keep_order(monkeypatch):
    fake = hbf.install(monkeypatch)
    fake.route("POST", "https://a.example.com/", handler=lambda r: (201, {"echo": r.json}), prefix=True)
    async with aiohttp.ClientSession() as s:
        async with s.post("https://a.example.com/1", json={"k": 1}) as r:
            assert r.status == 201 and await r.json() == {"echo": {"k": 1}}
        await s.post("https://a.example.com/2", json={"k": 2})
    assert [tuple(r) for r in fake.requests] == [
        ("POST", "https://a.example.com/1", {"k": 1}, None),
        ("POST", "https://a.example.com/2", {"k": 2}, None),
    ]


async def test_500_makes_todo_raise_and_gmail_raise_for_status(monkeypatch):
    fake = hbf.install(monkeypatch)
    fake.route("GET", f"{_GRAPH}/me/todo/lists", 500, "down")
    with pytest.raises(ValueError, match="500"):
        await _todo().list_task_lists("u1")
    fake.route("GET", "https://gmail.googleapis.com/gmail/v1/users/me/messages", 500, {})
    with pytest.raises(aiohttp.ClientResponseError):
        await GmailProviderAdapter("cid", "sec").list_emails(creds("u1", "gmail"))


async def test_response_surface(monkeypatch):
    fake = hbf.install(monkeypatch)
    fake.route("GET", "https://a.example.com/r", 200, b"raw")
    async with aiohttp.ClientSession() as s:
        async with s.get("https://a.example.com/r?q=1") as r:
            assert r.ok and await r.read() == b"raw"
            r.raise_for_status()


async def test_patch_delete_and_text_body(monkeypatch):
    fake = hbf.install(monkeypatch)
    fake.route("PATCH", "https://a.example.com/1", 200, {"p": 1})
    fake.route("DELETE", "https://a.example.com/1", 204, "")
    async with aiohttp.ClientSession() as s:
        async with s.patch("https://a.example.com/1", json={"a": 1}) as r:
            assert await r.json() == {"p": 1} and await r.text() == '{"p": 1}'
        async with s.delete("https://a.example.com/1") as r:
            assert r.status == 204
    assert [r.method for r in fake.requests] == ["PATCH", "DELETE"]


_GM = "https://gmail.googleapis.com/gmail/v1/users/me/messages"


async def test_gmail_500_on_one_message_is_skipped_without_crashing(monkeypatch):
    fake = hbf.install(monkeypatch)
    fake.route("GET", _GM, 200, {"messages": [{"id": "m1"}]})
    fake.route("GET", f"{_GM}/m1", 500, {})
    adapter = GmailProviderAdapter(client_id="cid", client_secret="sec")
    assert await adapter.list_emails(creds("u1", "gmail")) == ([], None)
    assert [r.url for r in fake.requests] == [_GM, f"{_GM}/m1"] and fake.unrouted == []


async def test_gmail_unrouted_message_fetch_is_swallowed_but_recorded(monkeypatch):
    fake = hbf.install(monkeypatch)
    fake.route("GET", _GM, 200, {"messages": [{"id": "m1"}]})
    adapter = GmailProviderAdapter(client_id="cid", client_secret="sec")
    assert await adapter.list_emails(creds("u1", "gmail")) == ([], None)
    assert [r.url for r in fake.unrouted] == [f"{_GM}/m1"]


async def test_task_create_does_not_hit_the_list_create_route(monkeypatch):
    from src.domain.task import TaskCreate
    fake = hbf.install(monkeypatch)
    fake.todo_empty_account()
    fake.route("POST", f"{_GRAPH}/me/todo/lists/l1/tasks", 201,
               {"id": "t1", "title": "buy milk", "status": "notStarted"})
    task = await _todo().create_task("u1", TaskCreate(title="buy milk", list_id="l1"))
    assert task.title == "buy milk"
    assert fake.requests_to("POST", f"{_GRAPH}/me/todo/lists/l1/tasks")[0].json["title"] == "buy milk"
    assert fake.unrouted == []


async def test_unknown_list_is_a_404_not_an_empty_list(monkeypatch):
    fake = hbf.install(monkeypatch)
    fake.todo_empty_account()
    fake.route("GET", f"{_GRAPH}/me/todo/lists/zz/tasks", 404, "nope")
    with pytest.raises(ValueError, match="Not found"):
        await _todo().list_tasks("u1", list_id="zz")


async def test_unknown_list_without_a_route_is_loud(monkeypatch):
    fake = hbf.install(monkeypatch)
    fake.todo_empty_account()
    with pytest.raises(aiohttp.ClientConnectionError):
        await _todo().list_tasks("u1", list_id="zz")
    assert fake.unrouted[-1].url.split("?")[0] == f"{_GRAPH}/me/todo/lists/zz/tasks"


async def test_json_of_a_text_body_needs_content_type_none(monkeypatch):
    hbf.install(monkeypatch).route("GET", "https://a.example.com/t", 200, "ok")
    async with aiohttp.ClientSession() as s:
        async with s.get("https://a.example.com/t") as r:
            with pytest.raises(aiohttp.ContentTypeError):
                await r.json()
            assert await r.text() == "ok"


async def test_json_with_content_type_none_parses_a_json_string(monkeypatch):
    hbf.install(monkeypatch).route("GET", "https://a.example.com/t", 200, '{"a": 1}')
    async with aiohttp.ClientSession() as s:
        async with s.get("https://a.example.com/t") as r:
            assert await r.json(content_type=None) == {"a": 1}


async def test_form_data_is_recorded_and_error_prints(monkeypatch):
    fake = hbf.install(monkeypatch)
    fake.route("POST", "https://a.example.com/f", 400, {})
    async with aiohttp.ClientSession() as s:
        async with s.post("https://a.example.com/f", data={"k": "v"}) as r:
            with pytest.raises(aiohttp.ClientResponseError) as ei:
                r.raise_for_status()
    assert fake.requests[0].data == {"k": "v"}
    assert "400" in str(ei.value)
