"""The consent page for a grant of the task queue's worker resource (EXTERNAL_TASK_QUEUE_RFC §5.4 "OAuth"): it lists the
user's own active workers as radio buttons (escaped, none picked in advance), offers Deny only when there is nothing to
pick (no worker, the flag off, a locked account, no task queue), and its POST reads the workers again: a worker that is
not one of them is 400 with no code; a picked worker is stamped on the code.

Real services: `ExternalTaskService` and `MCPAuthorizationService` of `test_cabinet_external_worker_grants.GrantSite`.
Alex Doe is `usr-1`; `usr-2` is another user with the flag.
"""
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from quart import Quart

from src.domain.mcp import MCPClient
from src.services.mcp_authorization_service import AuthorizationParams
from src.web.mcp_consent_app import build_mcp_consent_blueprint
from tests.unit.web.test_cabinet_external_worker_grants import FACTS, REDIRECT, TASKS, GrantSite, _add

_MARKUP = "<script>alert(1)</script>"


class ConsentSite(GrantSite):
    def __init__(self, with_queue: bool = True) -> None:
        super().__init__()
        self.consent_app = Quart("consent")
        self.consent_app.register_blueprint(build_mcp_consent_blueprint(
            self.mcp, self.kit.verifier, external_tasks=self.service if with_queue else None))
        self.consent_http = self.consent_app.test_client()

    async def request(self, resource=TASKS) -> str:
        client = await self.repo.get_client("cli-a")
        url = await self.mcp.build_consent_redirect(client, AuthorizationParams(
            redirect_uri=REDIRECT, code_challenge="c" * 43, scopes=[], state="st", resource=resource))
        return url.split("req=", 1)[1]

    async def page(self, auth, req: str):
        resp = await self.consent_http.get("/mcp/consent", query_string={"req": req}, headers=auth)
        return resp.status_code, (await resp.get_data()).decode()

    async def approve(self, auth, req: str, **form):
        resp = await self.consent_http.post("/mcp/consent", form={"req": req, "action": "approve", **form},
                                            headers=auth)
        return resp.status_code, resp.headers.get("location")


async def _site(with_queue: bool = True) -> ConsentSite:
    s = ConsentSite(with_queue)
    s.alex = await s.user("usr-1", "acc-1")
    s.other = await s.user("usr-2", "acc-2")
    await s.repo.save_client(MCPClient(client_id="cli-a", client_secret="s", client_name="Claude",
                                       redirect_uris=[REDIRECT], scope="user_context drive_read external_tasks",
                                       created_at=datetime.now(timezone.utc)))
    return s


@pytest.fixture
async def site():
    return await _site()


def _radios(body: str) -> list:
    return re.findall(r'<input type="radio" name="worker_id" value="([^"]*)" required />', body)


async def test_the_page_lists_only_the_users_active_workers_and_picks_none(site):
    research = await _add(site)
    watch = await _add(site, label="calendar-watch")
    removed = await _add(site, label="old-one")
    await site.call(site.alex, "DELETE", f"/api/external-workers/{removed}")
    await _add(site, site.other, label="theirs")
    status, body = await site.page(site.alex, await site.request())
    assert status == 200
    assert "Authorize task connector" in body and "<strong>Claude</strong>" in body
    assert sorted(_radios(body)) == sorted([research, watch])
    assert "checked" not in body
    assert "web research for home projects" in body and "theirs" not in body and "old-one" not in body
    assert 'value="approve"' in body and 'value="deny"' in body
    assert "biographical facts" not in body and "read your drives" not in body


async def test_a_label_and_a_description_are_escaped(site):
    await site.call(site.alex, "POST", "/api/external-workers", {"label": "research", "description": _MARKUP})
    _, body = await site.page(site.alex, await site.request())
    assert "<script>" not in body and "&lt;script&gt;alert(1)&lt;/script&gt;" in body


async def test_no_worker_offers_deny_only_with_the_reason(site):
    _, body = await site.page(site.alex, await site.request())
    assert _radios(body) == [] and 'value="approve"' not in body and 'value="deny"' in body
    assert "You have no workers yet" in body


async def test_the_flag_off_offers_deny_only(site):
    await _add(site)
    await site.set_flag("usr-1", False)
    _, body = await site.page(site.alex, await site.request())
    assert _radios(body) == [] and 'value="approve"' not in body
    assert "not switched on for your account" in body


async def test_a_locked_account_offers_deny_only(site):
    research = await _add(site)
    req = await site.request()
    await site.lock("usr-1")
    status, body = await site.page(site.alex, req)
    assert status == 200 and 'value="approve"' not in body and "This account is locked" in body
    assert await site.approve(site.alex, req, worker_id=research) == (400, None)


async def test_no_task_queue_offers_deny_only():
    s = await _site(with_queue=False)
    _, body = await s.page(s.alex, await s.request())
    assert _radios(body) == [] and 'value="approve"' not in body
    assert "not available on this server" in body
    status, location = await s.approve(s.alex, await s.request(), worker_id="wrk-1")
    assert (status, location) == (400, None)


async def test_a_facts_grant_shows_no_worker_picker(site):
    await _add(site)
    _, body = await site.page(site.alex, await site.request(FACTS))
    assert _radios(body) == [] and "Authorize MCP connector" in body and 'value="approve"' in body


async def test_approve_with_an_own_active_worker_stamps_it_on_the_code(site):
    research = await _add(site)
    status, location = await site.approve(site.alex, await site.request(), worker_id=research)
    assert status == 302
    query = parse_qs(urlsplit(location).query)
    assert query["state"] == ["st"]
    assert (await site.repo.get_auth_code(query["code"][0])).worker_id == research


async def test_approve_with_a_missing_foreign_or_removed_worker_is_400_with_no_code(site):
    await _add(site)
    theirs = await _add(site, site.other, label="theirs")
    removed = await _add(site, label="old-one")
    await site.call(site.alex, "DELETE", f"/api/external-workers/{removed}")
    req = await site.request()
    for form in ({}, {"worker_id": ""}, {"worker_id": theirs}, {"worker_id": removed}, {"worker_id": "wrk-404"}):
        assert await site.approve(site.alex, req, **form) == (400, None), form
    assert site.mcp_db.collection("a").docs == {}


async def test_approve_after_the_flag_went_off_is_400_with_no_code(site):
    research = await _add(site)
    req = await site.request()
    await site.set_flag("usr-1", False)
    assert await site.approve(site.alex, req, worker_id=research) == (400, None)
    assert site.mcp_db.collection("a").docs == {}


async def test_deny_needs_no_worker(site):
    resp = await site.consent_http.post("/mcp/consent", form={"req": await site.request(), "action": "deny"},
                                        headers=site.alex)
    assert resp.status_code == 302 and "error=access_denied" in resp.headers["location"]
