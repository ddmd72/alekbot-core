"""The tasks MCP resource `/tasks-mcp` on worker keys, end to end over the real streamable-HTTP app
(EXTERNAL_TASK_QUEUE_RFC §5.4, §5.7, §6.5; plan slice 3.1).

The world is `tests/support/task_mcp_world.py` (the REST endpoint's world with the tasks app on top); results are read
from `tools/call` over HTTP and texts are markers, never the tokens' wording. The REST blueprint over the same service
and limiter stands beside the app where the two share a bucket.

Alex Doe is `usr-1` (account `acc-1`) with the workers `research` and `calendar-watch`; `usr-2` is another user.
"""
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, List

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from quart import Quart

from src.domain.external_task_entry import TaskEntry
from src.composition import tasks_mcp_setup as tms
from src.composition.mcp_setup import SCOPE_OF, TASKS_SCOPE
from src.domain import external_task_mcp_contract as c
from src.domain.external_task import FILE_LINK_TTL_S, LEASE_S, RESULT_MAX_BYTES, ExternalTaskStatus
from src.domain.external_task_refusal import ExternalTaskRefused, TaskRefusal
from src.domain.external_worker_key import WORKER_KEY_PREFIX
from src.domain.mcp import ACCOUNT_LOCKED_TOOL_ERROR
from src.domain.mcp_resource import MCPResource, ScopeRefused, bind_resource, drive_resource_uri
from src.domain.secret_digest import fingerprint
from src.domain.user import InactiveReason
from src.domain.voice_mcp_token import McpTokenKind
from src.services.connector_text_service import ConnectorTextService
from src.web import external_task_api as api
from tests.support.task_file_fakes import CERTIFICATE, CERTIFICATE_REF, PLAN, PLAN_REF, InMemoryMediaStorage
from tests.support.task_mcp_texts import RESULT_TOKEN, marker
from tests.support.task_mcp_world import (
    AUTH_CONFIG,
    RATES,
    RESULT,
    TASKS_URI,
    TEXT,
    TITLE,
    UNKNOWN_KEY,
    USERS,
    World,
    err,
    ok,
)

NEUTRAL = marker(c.REFUSED)


class RecordingLogger:
    def __init__(self) -> None:
        self.lines: List[str] = []

    def _add(self, message: str, *a: Any, **k: Any) -> None:
        self.lines.append(message % a if a else message)

    info = warning = error = debug = _add


class Sink:
    def __init__(self) -> None:
        self.posts: List[str] = []

    async def post(self, text: str) -> None:
        self.posts.append(text)


@pytest.fixture
async def world() -> World:
    w = await World().seed()
    w.initial = await w.texts.texts()
    return w


@pytest.fixture
def logs(monkeypatch) -> RecordingLogger:
    recorder = RecordingLogger()
    monkeypatch.setattr(tms, "logger", recorder)
    return recorder


# -------------------------------------------------------------------------------------------------- the verifier

class TestVerifier:
    def verifier(self, world: World) -> tms.TasksTokenVerifier:
        return tms.TasksTokenVerifier(world.service, world.gate, TASKS_URI)

    async def test_a_live_key_is_a_worker_token_for_its_worker_with_the_tasks_scope_and_no_key_in_it(self, world):
        token = await self.verifier(world).verify_token(world.key)
        assert (token.kind, token.user_id, token.account_id, token.worker_id) == (
            McpTokenKind.WORKER_KEY, "usr-1", "acc-1", world.research.worker_id)
        assert (token.scopes, token.expires_at, token.resource) == ([TASKS_SCOPE], None, TASKS_URI)
        assert token.token == world.service.key_digest(world.key) and world.key not in token.model_dump_json()

    async def test_a_malformed_key_is_refused_with_no_store_read(self, world):
        for bad in (WORKER_KEY_PREFIX, WORKER_KEY_PREFIX + "short", WORKER_KEY_PREFIX + "!" * 43):
            assert await self.verifier(world).verify_token(bad) is None
        assert world.workers.get_key_reads == 0

    async def test_a_credential_without_the_key_prefix_is_refused_with_no_store_read(self, world):
        for other in ("eyJhbGciOiJIUzI1NiJ9.e30.sig", "", world.key[len(WORKER_KEY_PREFIX):]):
            assert await self.verifier(world).verify_token(other) is None
        assert world.workers.get_key_reads == 0

    async def test_an_unknown_and_a_revoked_key_are_refused(self, world):
        assert await self.verifier(world).verify_token(UNKNOWN_KEY) is None
        await world.service.revoke_key("usr-1", world.research.worker_id, world.service.key_digest(world.key))
        assert await self.verifier(world).verify_token(world.key) is None
        assert await self.verifier(world).verify_token(world.watch_key) is not None

    async def test_a_locked_user_a_switched_off_flag_and_a_revoked_worker_are_refused(self, world):
        await world.db.collection(USERS).document("usr-1").update(
            {"is_active": False, "inactive_reason": InactiveReason.DURESS.value})
        assert await self.verifier(world).verify_token(world.key) is None
        assert await self.verifier(world).verify_token(world.their_key) is not None
        w2 = await World().seed()
        await w2.flag("usr-1", False)
        assert await self.verifier(w2).verify_token(w2.key) is None
        w3 = await World().seed()
        await w3.service.revoke_worker("usr-1", w3.research.worker_id)
        assert await self.verifier(w3).verify_token(w3.key) is None
        assert await self.verifier(w3).verify_token(w3.watch_key) is not None

    async def test_a_user_off_the_whitelist_is_refused(self, world):
        world.whitelist.entry.allowed_emails.discard("alex@example.com")
        assert await self.verifier(world).verify_token(world.key) is None
        assert await self.verifier(world).verify_token(world.their_key) is not None

    async def test_a_refused_key_is_401_at_the_door(self, world, logs):
        for key in (UNKNOWN_KEY, "not-a-key", None):
            resp = await world.post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, key)
            assert resp.status_code == 401, key
        assert UNKNOWN_KEY not in "\n".join(logs.lines)


# -------------------------------------------------------------------------------------------------- the tools

class TestTools:
    async def test_the_six_tools_are_listed_with_their_texts_and_only_the_listing_is_read_only(self, world):
        tools = await world.list_tools()
        assert [t["name"] for t in tools] == list(c.TOOL_NAMES)
        for tool in tools:
            assert tool["description"] == marker(tool["name"])
            for name, prop in tool["inputSchema"]["properties"].items():
                assert prop["description"] == marker(f"{tool['name']}.{name}")
        annotations = {t["name"]: t.get("annotations") for t in tools}
        assert annotations.pop("list_tasks")["readOnlyHint"] is True
        assert set(annotations.values()) == {None}

    async def test_the_schema_advertises_no_constraint(self, world):
        for tool in await world.list_tools():
            schema = tool["inputSchema"]
            assert schema.get("required", []) == [], tool["name"]
            for prop in schema["properties"].values():
                assert set(prop) == {"type", "default", "title", "description"}
                assert (prop["type"], prop["default"]) == ("string", "")

    async def test_the_instructions_come_from_the_tool_texts(self, world):
        options = world.app()._mcp_server.create_initialization_options()
        assert options.instructions == marker("instructions")

    async def test_list_shows_this_workers_queued_and_leased_tasks_and_never_a_held_one(self, world):
        first = await world.create()
        second = await world.create()
        held = await world.create(entry=TaskEntry.CHAT)
        await world.create(label="calendar-watch")
        await world.create(user_id="usr-2")
        await world.claim()
        text = ok(await world.call("list_tasks"))
        lines = text.split("\n")
        assert lines[0] == "<LIST_HEAD 2>"
        assert lines[1].startswith("<LIST_LEASED") and first.task_id in lines[1] and TITLE in lines[1]
        assert lines[2].startswith("<LIST_QUEUED") and second.task_id in lines[2]
        assert held.task_id not in text and TEXT not in text

    async def test_an_empty_list_is_a_success(self, world):
        assert ok(await world.call("list_tasks")) == "<LIST_EMPTY>"

    async def test_claim_gives_provenance_first_then_the_title_then_the_plain_brief(self, world):
        created = await world.create()
        claimed = await world.claim()
        task = await world.tasks.get("usr-1", created.task_id)
        assert claimed["text"].split("\n") == [
            f"<CLAIMED {task.created_at.isoformat()} {task.lease.lease_id} {task.lease.until.isoformat()} "
            f"{created.task_id}>", "<WRITTEN_BY>", f"<TITLE {TITLE}>", "<TASK_TEXT>", TEXT]
        assert task.status is ExternalTaskStatus.LEASED and task.worker_id == world.research.worker_id

    async def test_claim_with_nothing_waiting_is_a_success_never_an_error(self, world):
        await world.create(entry=TaskEntry.CHAT)       # a held task is not claimable
        await world.create(label="calendar-watch")
        assert ok(await world.call("claim_task")) == "<NO_TASK>"

    async def test_renew_submit_and_its_replay(self, world):
        await world.create()
        claimed = await world.claim()
        world.now += timedelta(seconds=60)
        renewed = ok(await world.call("renew_lease", task_id=claimed["task_id"], lease_id=claimed["lease_id"]))
        task = await world.tasks.get("usr-1", claimed["task_id"])
        assert renewed == f"<RENEWED {task.lease.until.isoformat()} {claimed['task_id']}>"
        args = dict(task_id=claimed["task_id"], lease_id=claimed["lease_id"], result=RESULT)
        submitted = ok(await world.call("submit_task_result", **args))
        assert submitted.startswith(f"<SUBMITTED {len(RESULT)} ") and submitted.endswith(f" {claimed['task_id']}>")
        assert ok(await world.call("submit_task_result", **args)) == submitted
        task = await world.tasks.get("usr-1", claimed["task_id"])
        assert task.status is ExternalTaskStatus.DONE
        assert task.result_meta.submitted_via.value == "key" and task.result_meta.client_id is None
        assert await world.tasks.get_result("usr-1", claimed["task_id"]) == RESULT

    async def test_another_result_after_done_is_refused_with_its_code(self, world):
        await world.create()
        claimed = await world.claim()
        args = dict(task_id=claimed["task_id"], lease_id=claimed["lease_id"])
        ok(await world.call("submit_task_result", result=RESULT, **args))
        assert err(await world.call("submit_task_result", result="another body", **args)) == \
            "<E_ALREADY_SUBMITTED already_submitted>"
        assert await world.tasks.get_result("usr-1", claimed["task_id"]) == RESULT

    async def test_release_puts_the_task_back_and_its_lease_is_then_stale(self, world):
        await world.create()
        claimed = await world.claim()
        args = dict(task_id=claimed["task_id"], lease_id=claimed["lease_id"])
        assert ok(await world.call("release_task", **args)) == f"<RELEASED {claimed['task_id']}>"
        assert (await world.tasks.get("usr-1", claimed["task_id"])).status is ExternalTaskStatus.QUEUED
        assert err(await world.call("renew_lease", **args)) == "<E_STALE_LEASE stale_lease>"

    async def test_fail_closes_the_task_with_the_reason_and_an_empty_reason_is_none(self, world):
        await world.create()
        await world.create()
        first, second = await world.claim(), await world.claim()
        assert ok(await world.call("fail_task", task_id=first["task_id"], lease_id=first["lease_id"],
                                   reason="the shop is closed")) == f"<FAILED {first['task_id']}>"
        ok(await world.call("fail_task", task_id=second["task_id"], lease_id=second["lease_id"]))
        one, two = (await world.tasks.get("usr-1", first["task_id"])), (await world.tasks.get("usr-1", second["task_id"]))
        assert one.status is two.status is ExternalTaskStatus.FAILED
        assert (one.failure.reason, two.failure.reason) == ("the shop is closed", None)

    async def test_a_lapsed_lease_is_not_renewed(self, world):
        await world.create()
        claimed = await world.claim()
        world.now += timedelta(hours=1)       # past the lease, well before the task's deadline
        assert err(await world.call("renew_lease", task_id=claimed["task_id"], lease_id=claimed["lease_id"])) == \
            "<E_LEASE_EXPIRED lease_expired>"

    @pytest.mark.parametrize("tool,extra", [("renew_lease", {}), ("submit_task_result", {"result": RESULT}),
                                            ("release_task", {}), ("fail_task", {"reason": "no"})])
    async def test_an_empty_id_gets_a_correctable_line_and_nothing_changes(self, world, tool, extra):
        await world.create()
        claimed = await world.claim()
        assert err(await world.call(tool, task_id="  ", lease_id=claimed["lease_id"], **extra)) == \
            "<ID_REQUIRED task_id>"
        assert err(await world.call(tool, task_id=claimed["task_id"], **extra)) == "<ID_REQUIRED lease_id>"
        assert err(await world.call(tool, **extra)) == "<ID_REQUIRED task_id>"
        assert (await world.tasks.get("usr-1", claimed["task_id"])).status is ExternalTaskStatus.LEASED

    async def test_none_for_a_result_or_a_reason_reaches_the_service_as_empty(self, world):
        await world.create()
        claimed = await world.claim()
        args = dict(task_id=claimed["task_id"], lease_id=claimed["lease_id"])
        assert err(await world.call("submit_task_result", result=None, **args)) == "<E_INVALID_TEXT invalid_text>"
        ok(await world.call("fail_task", reason=None, **args))

    @pytest.mark.slow
    async def test_a_result_at_the_cap_goes_through_the_real_app_and_one_byte_more_is_too_large(self, world):
        await world.create()
        claimed = await world.claim()
        args = dict(task_id=claimed["task_id"], lease_id=claimed["lease_id"])
        assert err(await world.call("submit_task_result", result="x" * (RESULT_MAX_BYTES + 1), **args)) == \
            f"<E_TOO_LARGE too_large {RESULT_MAX_BYTES}>"
        text = ok(await world.call("submit_task_result", result="x" * RESULT_MAX_BYTES, **args))
        assert text.startswith(f"<SUBMITTED {RESULT_MAX_BYTES} ")


# -------------------------------------------------------------------------------------------------- tenancy (§6.5)

class TestTenancy:
    @pytest.mark.slow
    async def test_another_users_task_and_another_workers_task_answer_the_same_as_a_missing_one(self, world):
        await world.create(user_id="usr-2")
        await world.create(label="calendar-watch")
        theirs = await world.claim(world.their_key)
        watch = await world.claim(world.watch_key)
        answers = set()
        for other in (theirs, watch, {"task_id": "missing", "lease_id": theirs["lease_id"]}):
            for tool, extra in (("renew_lease", {}), ("submit_task_result", {"result": RESULT}),
                                ("release_task", {}), ("fail_task", {})):
                answers.add(err(await world.call(tool, task_id=other["task_id"], lease_id=other["lease_id"],
                                                 **extra)))
        assert answers == {"<E_NOT_FOUND not_found>"}
        assert (await world.tasks.get("usr-2", theirs["task_id"])).status is ExternalTaskStatus.LEASED
        assert (await world.tasks.get("usr-1", watch["task_id"])).status is ExternalTaskStatus.LEASED

    async def test_a_held_task_is_not_found_by_every_worker_tool(self, world):
        held = await world.create(entry=TaskEntry.CHAT)
        for tool in ("renew_lease", "release_task", "fail_task", "submit_task_result"):
            extra = {"result": RESULT} if tool == "submit_task_result" else {}
            assert err(await world.call(tool, task_id=held.task_id, lease_id="L", **extra)) == \
                "<E_NOT_FOUND not_found>"


# -------------------------------------------------------------------------------------------------- each call's checks

class TestCallChecks:
    async def test_a_lock_after_the_key_was_verified_answers_the_lock_text_before_any_operation(self, world, logs):
        await world.create()

        class Locked:
            async def is_inactive(self, user_id: str) -> bool:
                return True

        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "claim_task", "arguments": {}}}
        res = (await world.rpc(body, inactive_users=Locked()))["result"]
        assert err(res) == ACCOUNT_LOCKED_TOOL_ERROR
        assert [t.status for t in world.tasks.tasks.values()] == [ExternalTaskStatus.QUEUED]
        assert any("status=account_locked" in line for line in logs.lines)
        assert world.rate_docs() == 0

    @pytest.mark.parametrize("code", [TaskRefusal.NOT_ENABLED, TaskRefusal.WORKER_REVOKED])
    async def test_a_flag_or_worker_change_after_the_key_was_verified_is_the_neutral_line(self, world, monkeypatch,
                                                                                          code):
        async def refused(*_a, **_k):
            raise ExternalTaskRefused(code)

        monkeypatch.setattr(world.service, "list_for_worker", refused)
        assert err(await world.call("list_tasks")) == NEUTRAL

    async def test_a_failure_logs_its_type_only_and_answers_a_generic_tool_error(self, world, monkeypatch, logs):
        async def boom(*_a, **_k):
            raise RuntimeError(f"store down while reading {TEXT}")

        monkeypatch.setattr(world.service, "claim", boom)
        text = err(await world.call("claim_task"))
        assert "RuntimeError" in text and TEXT not in text
        assert any("failed: RuntimeError" in line for line in logs.lines)
        assert TEXT not in "\n".join(logs.lines)

    async def test_a_failing_lock_check_answers_the_type_only(self, world, logs):
        await world.create()

        class Broken:
            async def is_inactive(self, user_id: str) -> bool:
                raise RuntimeError(f"store down while reading {TEXT}")

        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "claim_task", "arguments": {}}}
        text = err((await world.rpc(body, inactive_users=Broken()))["result"])
        assert "RuntimeError" in text and TEXT not in text
        assert any("failed: RuntimeError" in line for line in logs.lines)
        assert TEXT not in "\n".join(logs.lines)
        assert [t.status for t in world.tasks.tasks.values()] == [ExternalTaskStatus.QUEUED]

    async def test_texts_lost_after_the_listing_answer_the_no_texts_line(self, world):
        class FailsOnSecondRead:
            def __init__(self, texts: ConnectorTextService) -> None:
                self.texts_service, self.reads = texts, 0

            async def texts(self):
                self.reads += 1
                if self.reads > 1:
                    from src.domain.exceptions import ConnectorTextsUnavailable
                    raise ConnectorTextsUnavailable("connector texts could not be loaded")
                return await self.texts_service.texts()

        world.texts_override = FailsOnSecondRead(world.texts)
        assert err(await world.call("list_tasks")) == tms.NO_TEXTS

    def test_a_request_without_a_worker_credential_is_a_tool_error(self):
        for user in (None, SimpleNamespace(access_token=None),
                     SimpleNamespace(access_token=tms.AlekAccessToken(
                         token="t", client_id="c", scopes=["drive_read"], user_id="u1", account_id="a1"))):
            ctx = SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(user=user)))
            with pytest.raises(ToolError):
                tms._worker_token(ctx, "list_tasks")


# -------------------------------------------------------------------------------------------------- rate limits

class TestRateLimits:
    async def test_calls_over_the_limit_get_the_wait_and_another_worker_does_not(self, world, monkeypatch):
        monkeypatch.setattr(tms, "CALLS", (("calls", 2),))
        for _ in range(2):
            ok(await world.call("list_tasks"))
        text = err(await world.call("list_tasks"))
        assert text.startswith("<RATE_LIMITED ") and int(text[len("<RATE_LIMITED "):-1]) > 0
        ok(await world.call("list_tasks", world.watch_key))

    async def test_claims_and_submits_have_their_own_limits(self, world, monkeypatch):
        monkeypatch.setattr(tms, "CLAIMS", (("calls", 100), ("claims", 1)))
        monkeypatch.setattr(tms, "SUBMITS", (("calls", 100), ("submits", 1)))
        await world.create()
        await world.create()
        claimed = await world.claim()
        assert err(await world.call("claim_task")).startswith("<RATE_LIMITED ")
        args = dict(task_id=claimed["task_id"], lease_id=claimed["lease_id"])
        ok(await world.call("submit_task_result", result=RESULT, **args))
        assert err(await world.call("submit_task_result", result=RESULT, **args)).startswith("<RATE_LIMITED ")
        ok(await world.call("list_tasks"))

    async def test_the_calls_bucket_is_the_rest_endpoints_own(self, world, monkeypatch):
        monkeypatch.setattr(tms, "CALLS", (("calls", 2),))
        monkeypatch.setattr(api, "CALLS_PER_WINDOW", 2)
        app = Quart("rest")
        app.register_blueprint(api.create_external_task_api_blueprint(
            world.service, limiter=world.limiter, user_repo=world.users, whitelist_repo=world.whitelist))
        rest = app.test_client()
        headers = {"Authorization": f"Bearer {world.key}", "X-Forwarded-For": "203.0.113.9"}
        assert (await rest.get("/external-tasks/v1/tasks", headers=headers)).status_code == 200
        ok(await world.call("list_tasks"))
        assert err(await world.call("list_tasks")).startswith("<RATE_LIMITED ")
        assert (await rest.get("/external-tasks/v1/tasks", headers=headers)).status_code == 429


# -------------------------------------------------------------------------------------------------- logs

async def test_the_log_lines_carry_no_text_result_reason_key_or_header(world, logs):
    await world.create()
    claimed = await world.claim()
    args = dict(task_id=claimed["task_id"], lease_id=claimed["lease_id"])
    ok(await world.call("renew_lease", **args))
    ok(await world.call("submit_task_result", result=RESULT, **args))
    await world.call("fail_task", reason="the shop is closed", **args)
    await world.post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, UNKNOWN_KEY)
    text = "\n".join(logs.lines)
    for secret in (TEXT, TITLE, RESULT, "the shop is closed", world.key, UNKNOWN_KEY, "Bearer",
                   world.service.key_digest(world.key), claimed["lease_id"]):
        assert secret not in text, secret
    worker = fingerprint(world.research.worker_id)
    tools = [line for line in logs.lines if line.startswith("[TaskMCP] tool=")]
    assert [line.split()[1] for line in tools] == ["tool=claim_task", "tool=renew_lease", "tool=submit_task_result",
                                                   "tool=fail_task"]
    assert all(f"user=usr-1 worker={worker} status=" in line and " ms=" in line for line in tools)
    assert tools[-1].split()[4] == "status=stale_lease"


# -------------------------------------------------------------------------------------------------- the build

class TestBuild:
    async def build(self, world: World, **overrides: Any):
        kwargs = dict(external_tasks=world.service, db_client=world.rates_db,
                      env_config=SimpleNamespace(worker_task_dedup_collection=RATES), user_repo=world.users,
                      whitelist_repo=world.whitelist, token_repo=world.store, alert_sink=world.sink,
                      inactive_users=world.inactive, auth_config=AUTH_CONFIG)
        kwargs.update(overrides)
        return await tms.build_tasks_mcp_or_none(**kwargs)

    @pytest.fixture(autouse=True)
    def sink(self, world):
        world.sink = Sink()

    async def test_the_build_serves_the_tasks_path_and_scope(self, world):
        app = await self.build(world)
        assert app is not None and app.settings.streamable_http_path == "/tasks-mcp"
        assert app.settings.auth.required_scopes == [TASKS_SCOPE]
        assert str(app.settings.auth.resource_server_url) == TASKS_URI

    async def test_no_task_queue_or_no_token_store_is_no_app(self, world):
        assert await self.build(world, external_tasks=None) is None
        assert await self.build(world, token_repo=None) is None

    async def test_a_missing_texts_token_is_no_app_with_one_alert(self, world):
        del world.store.tokens[RESULT_TOKEN]
        assert await self.build(world) is None
        assert len(world.sink.posts) == 1
        assert world.sink.posts[0].startswith("Task connector texts unavailable: ") and RESULT_TOKEN in world.sink.posts[0]

    async def test_a_tool_outside_the_contract_refuses_the_build(self, world, monkeypatch):
        monkeypatch.setattr(c, "TOOL_PARAMETERS", {**c.TOOL_PARAMETERS, "fail_task": ("task_id", "lease_id")})
        with pytest.raises(RuntimeError, match="fail_task"):
            world.app()
        assert await self.build(world) is None


def test_the_tasks_scope_is_in_scope_of_and_binds_only_where_it_is_served():
    # reviewer edit, task queue step 4.1 (RFC §5.4 "OAuth"): the pre-OAuth state (TASKS absent from SCOPE_OF) is
    # superseded; the scope string is in SCOPE_OF, and binding still fails closed without it or without its URI
    assert SCOPE_OF[MCPResource.TASKS] == TASKS_SCOPE == "external_tasks"
    uris = {MCPResource.FACTS: AUTH_CONFIG.mcp_resource_uri,
            MCPResource.DRIVE: drive_resource_uri(AUTH_CONFIG.mcp_resource_uri)}
    served = {**uris, MCPResource.TASKS: TASKS_URI}
    assert bind_resource(None, [TASKS_SCOPE], served, SCOPE_OF) == (MCPResource.TASKS, TASKS_URI, [TASKS_SCOPE])
    without_tasks = {k: v for k, v in SCOPE_OF.items() if k is not MCPResource.TASKS}
    with pytest.raises(ScopeRefused):
        bind_resource(None, [TASKS_SCOPE], served, without_tasks)
    with pytest.raises(ScopeRefused):
        bind_resource(None, [TASKS_SCOPE], uris, SCOPE_OF)


# ------------------------------------------------------------------------------------- task files (RFC §5.12)

@pytest.fixture
async def files_world() -> World:
    w = await World(files=True).seed()
    w.initial = await w.texts.texts()
    return w


def _link(text: str) -> str:
    return text.rstrip(">").rsplit(" ", 1)[-1]


class TestTaskFiles:
    async def test_a_claim_lists_the_files_after_the_title_and_before_the_brief_with_no_link(self, files_world):
        created = await files_world.create(files=[PLAN_REF, CERTIFICATE_REF])
        claimed = await files_world.claim()
        lines = claimed["text"].split("\n")
        assert lines[2:] == [f"<TITLE {TITLE}>", "<FILES_HEAD 2>",
                             f"<FILE_LINE {PLAN.mime} Tom's plan.docx 1 {len(PLAN.data)}>",
                             f"<FILE_LINE application/pdf birth-certificate.pdf 2 {len(CERTIFICATE.data)}>",
                             "<TASK_TEXT>", TEXT]
        assert claimed["task_id"] == created.task_id
        assert InMemoryMediaStorage.SIGNED_BASE not in claimed["text"] and "external_tasks/" not in claimed["text"]

    async def test_a_live_lease_gets_a_fresh_signed_link_on_every_call(self, files_world):
        await files_world.create(files=[PLAN_REF, CERTIFICATE_REF])
        claimed = await files_world.claim()
        first = ok(await files_world.call("get_task_file", task_id=claimed["task_id"], lease_id=claimed["lease_id"],
                                          file="2"))
        second = ok(await files_world.call("get_task_file", task_id=claimed["task_id"],
                                           lease_id=claimed["lease_id"], file=2))
        key = f"external_tasks/usr-1/{claimed['task_id']}/2-birth-certificate.pdf"
        assert first.startswith(f"<FILE_LINK application/pdf birth-certificate.pdf 2 {FILE_LINK_TTL_S} "
                                f"{len(CERTIFICATE.data)} {claimed['task_id']} {InMemoryMediaStorage.SIGNED_BASE}/{key}?")
        assert _link(first) != _link(second)
        assert files_world.bucket.signed == [(key, FILE_LINK_TTL_S), (key, FILE_LINK_TTL_S)]

    async def test_a_lapsed_lease_is_refused(self, files_world):
        await files_world.create(files=[PLAN_REF])
        claimed = await files_world.claim()
        files_world.now += timedelta(seconds=LEASE_S)
        assert err(await files_world.call("get_task_file", task_id=claimed["task_id"],
                                          lease_id=claimed["lease_id"], file="1")) == "<E_LEASE_EXPIRED lease_expired>"
        assert files_world.bucket.signed == []

    async def test_another_workers_task_a_held_task_and_a_final_task_are_not_found(self, files_world):
        await files_world.create(label="calendar-watch", files=[PLAN_REF])
        watch = await files_world.claim(files_world.watch_key)
        held = await files_world.create(entry=TaskEntry.CHAT, files=[PLAN_REF])
        await files_world.create(files=[PLAN_REF])
        mine = await files_world.claim()
        ok(await files_world.call("submit_task_result", task_id=mine["task_id"], lease_id=mine["lease_id"],
                                  result=RESULT))
        for task_id, lease_id in ((watch["task_id"], watch["lease_id"]), (held.task_id, "L"),
                                  (mine["task_id"], mine["lease_id"])):
            assert err(await files_world.call("get_task_file", task_id=task_id, lease_id=lease_id, file="1")) == \
                "<E_NOT_FOUND not_found>"
        assert files_world.bucket.signed == []

    async def test_a_missing_copy_is_file_gone_and_a_signing_failure_is_retryable(self, files_world):
        await files_world.create(files=[PLAN_REF, CERTIFICATE_REF])
        claimed = await files_world.claim()
        ids = {"task_id": claimed["task_id"], "lease_id": claimed["lease_id"]}
        del files_world.bucket.objects[f"external_tasks/usr-1/{claimed['task_id']}/1-Tom's plan.docx"]
        assert err(await files_world.call("get_task_file", file="1", **ids)) == "<E_FILE_GONE file_gone>"
        files_world.bucket.fail_sign = True
        assert err(await files_world.call("get_task_file", file="2", **ids)) == \
            "<E_LINK_UNAVAILABLE link_unavailable>"
        files_world.bucket.fail_sign = False
        assert ok(await files_world.call("get_task_file", file="2", **ids)).startswith("<FILE_LINK ")

    @pytest.mark.parametrize("file", ["3", "0", "", "plan", "-1", "1.5"])
    async def test_a_file_that_is_not_one_of_the_tasks_numbers_is_no_such_file(self, files_world, file):
        await files_world.create(files=[PLAN_REF, CERTIFICATE_REF])
        claimed = await files_world.claim()
        assert err(await files_world.call("get_task_file", task_id=claimed["task_id"], lease_id=claimed["lease_id"],
                                          file=file)) == "<E_NO_SUCH_FILE no_such_file>"

    async def test_an_empty_id_gets_the_correctable_line(self, files_world):
        assert err(await files_world.call("get_task_file", task_id="", lease_id="L", file="1")) == \
            "<ID_REQUIRED task_id>"

    async def test_the_log_lines_never_carry_the_link_or_the_files_name(self, files_world, logs):
        await files_world.create(files=[PLAN_REF])
        claimed = await files_world.claim()
        link = _link(ok(await files_world.call("get_task_file", task_id=claimed["task_id"],
                                               lease_id=claimed["lease_id"], file="1")))
        text = "\n".join(logs.lines)
        assert "tool=get_task_file" in text
        assert link not in text and "Tom's plan" not in text and "external_tasks/" not in text
