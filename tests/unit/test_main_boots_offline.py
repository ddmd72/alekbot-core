"""The real `main()` boots offline under the deploy's environment and serves the deployed surface (P9 design §5f, §5b).

`main()` runs in a sealed environment shaped like the `alek-bot-dev` deploy (`tests/support/prod_env.py`), with fakes at
the SDK boundaries only: Firestore, Cloud Tasks, Gemini embeddings, OpenAI retrieval, Google's OIDC key check, aiohttp
(Graph To Do and Gmail, installed after startup), Slack's `chat_postMessage` and the Anthropic SDK's `messages.stream`
(scripted). `_seed_rows` gives five rows their data (P9 hand-over). The patches name SDK modules and names `main` keeps,
so this file survives moving the construction out of `main()` unedited.

`hypercorn.asyncio.serve` is replaced by a probe: with the app built and started it records what the deployed process
would serve (log slice, `url_map`, the keyword arguments of every blueprint factory call) and sends every `/worker`
task type through the real route and OIDC gate. The probe only observes; a failure inside `serve` is swallowed by
`main()`, so assertions run after `main()` returns. Everything is read from one boot, each section is checked on its own
and the failures are reported together.

`UPDATE_PINS=1` rewrites the url_map pin from the booted app; review the diff. The boot test is `slow`, so the command is
`UPDATE_PINS=1 RUN_SLOW_TESTS=1 pytest tests/unit/test_main_boots_offline.py` (one file, no `-n`: a parallel self-test could
read the pin mid-rewrite).
"""
import ast
import inspect
import json
import logging
import os
import re
import socket
import types
import typing
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

import pytest
import quart

import main
from tests.support import prod_env, worker_triggers
from tests.support.cloud_tasks_fake import FakeCloudTasksClient
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import anthropic
import slack_sdk.web.async_client
from anthropic.types import Message as AnthropicMessage
from anthropic.types import ToolUseBlock, Usage

from src.config.environment import EnvironmentConfig
from src.domain.deep_research_notices import cancelled_notice, research_failed_notice
from src.domain.email import IndexingJob
from tests.support import http_boundary_fake
from tests.support.oauth_credentials_store import creds
from tests.support.prompt_seed import seed_agent_prompt

ROOT = Path(__file__).resolve().parents[2]
URL_MAP = Path(__file__).with_name("main_boots_offline_url_map.txt")
BLUEPRINT_FACTORY = re.compile(r"^(create|build)_\w+_blueprint$")

# A startup line that says a feature is off. Empty in production today; an entry needs a reason.
REASONED_STARTUP_LINES: Dict[str, str] = {}
STARTUP_LINE = re.compile(r"not configured|disabled|skipped|continue without", re.IGNORECASE)
# A /worker answer that means "this deployment cannot run that task type".
REFUSAL_BODY = re.compile(r"not configured|not ready|disabled", re.IGNORECASE)
REFUSAL_STATUSES = frozenset({501, 503})

# The five seeded rows (`_seed_rows`) and the outcomes `_check_seeded_rows` reads. Texts: invented universe, deep research topic.
DR_QUERY = "Heat pumps in Springfield"
DR_FAILED_REPLY = "The research on heat pumps in Springfield did not finish. Ask me again and I will start it over."
DR_CANCELLED_REPLY = "The research on heat pumps in Springfield was cancelled. Ask me again whenever you like."
DR_FAILED_SUMMARY = "Told Alex the heat pump research did not finish."
DR_CANCELLED_SUMMARY = "Told Alex the heat pump research was cancelled."
INDEXING_JOB_ID = "eijob001"   # 8 characters: the worker logs job_id[:8]
_GRAPH_URL = "https://graph.microsoft.com/v1.0"
_GMAIL_URL = "https://gmail.googleapis.com/gmail/v1/users/me"
SEEDED_HTTP = Counter({
    ("GET", f"{_GMAIL_URL}/messages"): 2,
    ("GET", f"{_GRAPH_URL}/me/todo/lists/l1"): 1,
    ("GET", f"{_GRAPH_URL}/me/todo/lists/l1/tasks"): 1,
    ("GET", f"{_GRAPH_URL}/me/todo/lists"): 1,
    ("POST", f"{_GRAPH_URL}/me/todo/lists"): 1,
    ("POST", f"{_GRAPH_URL}/subscriptions"): 1,
})


class Expect(NamedTuple):
    """What one task type must answer. `body=None`: the answer is Quart's error page, the cause is the logged exception."""
    status: int
    body: Optional[dict]
    log: Optional[str] = None      # a log record of this row contains it
    exception: Optional[str] = None  # a record of this row carries an exception whose repr contains it


# The payload of every enqueue-site type: the fields its handler reads. Scheduler types post their literal script body.
ENQUEUE_PAYLOADS: Dict[str, dict] = {
    "consolidation": {"user_id": "u1"},
    "companion_consolidation": {"session_id": "u1:C1"},
    "renew_task_subscriptions": {"user_id": "u1"},
    "execute_reminder": {"note_id": "n1", "user_id": "u1", "due_at": "2026-10-23T08:00:00+00:00"},
    "agent_execution": {"agent_id": "no_such_agent", "intent": "nothing", "query": "", "context": {"user_id": "u1", "account_id": "a1"}},
    "daily_email_review": {"user_id": "u1", "account_id": "a1"},
    "video_generation_polling": {"request_id": "req1", "user_id": "u1", "account_id": "a1", "session_id": "u1:C1", "attempt": 0},
    "telegram_update": {"update": {}},
    "email_indexing": {"job_id": INDEXING_JOB_ID},
    "deep_research_failed": {"user_id": "u1", "account_id": "a1", "session_id": "u1:C1", "query": DR_QUERY, "reason": "x", "origin_platform": "slack"},
    "deep_research_webhook_event": {"event_type": "response.cancelled", "job_id": "job1", "user_id": "u1", "account_id": "a1", "session_id": "u1:C1", "query": DR_QUERY, "origin_platform": "slack"},
    "setup_microsoft_todo": {"user_id": "u1"},
    "reindex_task_list": {"user_id": "u1", "list_id": "l1"},
    "fact_review": {"user_id": "u1", "account_id": "a1", "mode": "daily"},
    "interactive_choice": {"binding": {"kind": "fact_question", "id": "q1"}, "option_id": "still_true", "platform": "slack",
                           "platform_user_id": "U1", "message_ref": {"platform": "slack", "channel_id": "C1", "message_id": "1.1"},
                           "at": "2026-10-24T09:00:00+00:00"},
}

# The pinned answer of every task type, from the boot with the deploy's environment, an empty database and the rows' seed
# (`_seed_rows`: u1's To Do and Gmail credentials, one running indexing job, the `quick` prompt profile) (P9).
# Every row that is not a plain 200 names its cause; none of them is a configuration refusal.
MATRIX: Dict[str, Expect] = {
    # Cloud Scheduler jobs
    "billing_daily_summary": Expect(200, {"reported": 0}),
    "email_indexing_watchdog": Expect(200, {"marked_failed": 0, "status": "ok"}),
    # The janitor under the deploy's environment: writing (FACT_LIFECYCLE_SWEEP_DRY_RUN=0, the owner's switch of
    # 2026-10-27), an empty store.
    "fact_lifecycle_sweep": Expect(200, {"accounts": 0, "due": 0, "staled": 0, "would_stale": 0, "deferred": 0,
                                         "conflicts": 0, "no_decision": 0, "breaker_tripped": 0, "review_enqueued": 0,
                                         "purge_overdue": 0, "dry_run": False}),
    "fire_due_reminders": Expect(200, {"claim_lost": 0, "enqueued": 0, "skipped": 0}),
    "renew_all_task_subscriptions": Expect(200, {"enqueued": 1, "status": "ok"}),
    "repair_email_embeddings": Expect(200, {"has_more": False, "repaired": 0, "status": "ok"}),
    "start_daily_email_review": Expect(200, {"failed": 0, "started": 0}),
    "start_drive_sync": Expect(200, {"alerts": 0, "candidates": 0, "continuing": 0, "enqueued": 0, "errors": 0, "notices": 0, "skipped": 0}),
    "start_email_indexing": Expect(200, {"skipped": 1, "started": 0},
                                   log="[EmailIndexing] start_indexing_for_eligible_users: started=0, skipped=1"),
    "sweep_companion_consolidation": Expect(200, {"swept": 0}),
    "sweep_consolidation": Expect(200, {"swept": 0}),
    # enqueue sites
    "consolidation": Expect(200, {"status": "ok"}),
    "companion_consolidation": Expect(200, {"status": "ok"}),
    "renew_task_subscriptions": Expect(200, {"status": "ok"}),
    "execute_reminder": Expect(200, {"status": "note_gone"}),
    "telegram_update": Expect(200, {"ok": True}),
    # No agent of that name runs offline; the row still proves the dispatch and `ensure_agents_for_user`.
    "agent_execution": Expect(
        200, {"agent_id": "no_such_agent_u1", "error": "Unknown recipient: no_such_agent_u1", "intent": "nothing", "status": "failed"},
        log="No route found for recipient: no_such_agent_u1"),
    # Gmail credentials are seeded (for email_indexing); the Gmail fake lists no message.
    "daily_email_review": Expect(200, {"status": "no_emails"}, log="[Worker] daily_email_review: no emails in last 24h for u1"),
    # The xAI poll cannot connect offline: a DNS failure where there is no resolver, the socket guard where there is one.
    # Both reach the handler as the SDK's 'Connection error.', so the retry path runs; no connect may have succeeded.
    "video_generation_polling": Expect(
        200, {"attempt": 1, "status": "retry"}, log="[VideoGeneration] get_status failed, attempt=0: Connection error."),
    # A running job and Gmail credentials are seeded; the Gmail fake lists no message.
    "email_indexing": Expect(200, {"has_more": False, "status": "ok"},
                             log=f"Indexing job {INDEXING_JOB_ID} complete: stored=0, failed=0"),
    # The notice goes through Quick (the `quick` profile is seeded, Claude is scripted) to the origin channel.
    "deep_research_failed": Expect(200, {"status": "ok"}, log="[Notification] Sent to slack channel=C1 user=u1 kind=deep_research"),
    "deep_research_webhook_event": Expect(200, {"status": "ok"},
                                          log="[Notification] Sent to slack channel=C1 user=u1 kind=deep_research"),
    # To Do credentials are seeded; Graph is the HTTP fake's empty account.
    "setup_microsoft_todo": Expect(200, {"status": "ok"}, log="MS To Do setup complete for user u1"),
    "reindex_task_list": Expect(200, {"status": "ok"}, log="Reindexed 0 tasks for list l1, user u1"),
    # The seeded account has no owner: the review stops at its owner check.
    "fact_review": Expect(200, {"status": "not_owner"},
                          log="[FactReview] user u1 is not the owner of account a1 (account has no owner); nothing done"),
    # No platform user is seeded: the click is refused at the identity check, before any handler (never a 501).
    "interactive_choice": Expect(200, {"status": "unknown_user"},
                                 log="[InteractiveChoice] a choice from an unknown slack user; ignored"),
}


@dataclass
class Row:
    status: int
    body: Optional[dict]
    records: List[logging.LogRecord]
    connects: List[Tuple[Any, Optional[str]]] = field(default_factory=list)


@dataclass
class Boot:
    """Everything the probe read from the one boot."""
    startup: List[logging.LogRecord] = field(default_factory=list)
    shutdown_logged: bool = False
    rules: List[str] = field(default_factory=list)
    calls: List[Tuple[str, Callable, tuple, dict]] = field(default_factory=list)
    queue_calls: List[Tuple[List[str], List[str]]] = field(default_factory=list)
    rows: Dict[str, Row] = field(default_factory=dict)
    unknown: Optional[Tuple[int, Any]] = None
    anonymous: Optional[Tuple[int, Any]] = None
    worker_closure: Dict[str, Any] = field(default_factory=dict)
    served_is_quart: Optional[bool] = None
    mcp_discovery: Optional[Tuple[int, Any]] = None
    health_through_served: Optional[int] = None
    seeded: Optional[Tuple[Any, Any]] = None
    connects: List[Tuple[Any, Optional[str]]] = field(default_factory=list)
    recorded_names: List[str] = field(default_factory=list)
    probe_error: Optional[BaseException] = None
    # What the five seeded rows left behind (`_seed_rows`, read at the end of `_probe`).
    db: Any = None
    http: Any = None                                                   # the HttpFake
    slack_posts: List[Tuple[str, str]] = field(default_factory=list)   # (channel, text) of every chat_postMessage
    claude_calls: List[dict] = field(default_factory=list)             # kwargs of every AsyncMessages.stream call
    task_bodies: List[dict] = field(default_factory=list)
    history: List[Tuple[str, Optional[str], Optional[str]]] = field(default_factory=list)  # (role, text, full_text) in u1:C1
    todo_config: Any = None
    indexing_job: Any = None


def _main_blueprint_factories() -> List[Tuple[str, str]]:
    """`(module, name)` of every blueprint factory `main.py` imports, at module level or inside a function."""
    found = []
    for node in ast.walk(ast.parse((ROOT / "main.py").read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            found += [(node.module, a.name) for a in node.names if BLUEPRINT_FACTORY.match(a.name)]
    return found


def _main_blueprint_calls() -> set:
    """Names of the blueprint factories `main.py` calls, read from its source."""
    names = set()
    for node in ast.walk(ast.parse((ROOT / "main.py").read_text())):
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if BLUEPRINT_FACTORY.match(name):
                names.add(name)
    return names


def _record_blueprint_factories(monkeypatch, boot: Boot) -> None:
    """Wrap every factory where `main()` reads it: at its own module (function-local imports) and in `main` (module-level)."""
    import importlib
    for module_name, name in _main_blueprint_factories():
        module = importlib.import_module(module_name)
        original = getattr(module, name)

        def recording(*args: Any, __original=original, __name=name, **kwargs: Any) -> Any:
            boot.calls.append((__name, __original, args, kwargs))
            return __original(*args, **kwargs)

        monkeypatch.setattr(module, name, recording)
        if hasattr(main, name):
            monkeypatch.setattr(main, name, recording)


def _worker_closure(app: quart.Quart) -> Dict[str, Any]:
    view = app.view_functions["worker"]
    return dict(zip(view.__code__.co_freevars, (cell.cell_contents for cell in view.__closure__ or ())))


async def _post(client: Any, body: dict, token: Optional[str] = prod_env.VALID_TOKEN) -> Tuple[int, Any]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = await client.post("/worker", json=body, headers=headers)
    try:
        payload = await response.get_json()
    except Exception:
        payload = None
    return response.status_code, payload


async def _through_asgi(asgi_app: Any, path: str) -> Tuple[int, Any]:
    import httpx
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi_app), base_url="http://testserver") as client:
        response = await client.get(path)
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, None


async def _seed_rows(boot: Boot) -> None:
    """The data five rows need to answer 200: the `quick` prompt, u1's To Do and Gmail credentials and one running indexing job.

    Called from `_probe`, never from `_boot`: `test_worker_redelivery_route.py` replaces `_probe` and needs the unseeded 500 rows."""
    await seed_agent_prompt(boot.db, EnvironmentConfig(), "quick")
    worker = boot.worker_closure["worker_handler"]
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(hours=1)
    for provider in ("microsoft_todo", "gmail"):
        await worker._oauth_credentials.save_credentials(creds("u1", provider, token_expiry=expiry))
    await worker._email_indexing._job_repo.create_job(IndexingJob(
        job_id=INDEXING_JOB_ID, user_id="u1", account_id="a1", provider="gmail", triggered_by="scheduler",
        status="running", started_at=now, updated_at=now))


async def _probe(app: quart.Quart, served: Any, boot: Boot, caplog: pytest.LogCaptureFixture) -> None:
    boot.served_is_quart = isinstance(served, quart.Quart)
    boot.mcp_discovery = await _through_asgi(served, "/.well-known/oauth-authorization-server")
    boot.health_through_served = (await _through_asgi(served, "/health"))[0]
    boot.startup = list(caplog.records)
    boot.rules = sorted(
        f"{','.join(sorted(rule.methods - {'HEAD', 'OPTIONS'}))} {rule.rule}" for rule in app.url_map.iter_rules())
    boot.queue_calls = [(c.get_queue_calls, c.created_queues) for c in FakeCloudTasksClient.instances]
    boot.worker_closure = _worker_closure(app)

    oauth_kwargs = next(kw for name, _, _, kw in boot.calls if name == "create_oauth_blueprint")
    from src.domain.billing import BillingAccount
    from src.domain.user import UserProfile
    auth = oauth_kwargs["auth_service"]
    await auth.account_repo.create_account(BillingAccount(account_id="a1"))
    await auth.user_repo.create_user(UserProfile(user_id="u1", account_id="a1"))
    boot.seeded = (await auth.account_repo.get_account("a1"), await auth.user_repo.get_user("u1"))
    await _seed_rows(boot)

    client = app.test_client()
    bodies = worker_triggers.scheduler_task_types()
    payloads = {**bodies, **{t: {"task_type": t, **p} for t, p in ENQUEUE_PAYLOADS.items()}}
    for task_type, body in sorted(payloads.items()):
        start = len(caplog.records)
        connects = len(boot.connects)
        status, answer = await _post(client, body)
        boot.rows[task_type] = Row(status, answer, list(caplog.records[start:]), boot.connects[connects:])
    worker = boot.worker_closure["worker_handler"]
    boot.task_bodies = [b for c in FakeCloudTasksClient.instances for b in c.created_bodies()]
    session = await worker._notification._session_store.load_session("u1:C1")
    boot.history = [(m.role, p.text, p.full_text) for m in session.messages for p in m.parts]
    boot.todo_config = await worker._task_setup._task_config.get_config("u1")
    boot.indexing_job = await worker._email_indexing._job_repo.get_job(INDEXING_JOB_ID)
    boot.unknown = await _post(client, {"task_type": "no_such_type"})
    boot.anonymous = await _post(client, {"task_type": "fire_due_reminders"}, token=None)


async def _boot(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> Boot:
    boot = Boot()
    services = prod_env.install_offline_services(monkeypatch)
    boot.db = services.db
    monkeypatch.setattr(main, "init_telemetry", lambda *a, **k: None)
    monkeypatch.setattr(main, "build_memory_telemetry", lambda *a, **k: None)

    apps: List[quart.Quart] = []

    class RecordingQuart(quart.Quart):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            apps.append(self)

    monkeypatch.setattr("quart.Quart", RecordingQuart)

    guarded_connect = socket.socket.connect

    def recording_connect(sock: socket.socket, address: Any) -> Any:
        try:
            result = guarded_connect(sock, address)
        except BaseException as exc:
            boot.connects.append((address, type(exc).__name__))
            raise
        boot.connects.append((address, None))
        return result

    monkeypatch.setattr(socket.socket, "connect", recording_connect)
    _record_blueprint_factories(monkeypatch, boot)

    async def chat_post_message(self: Any, *, channel: str, text: Optional[str] = None, **kwargs: Any) -> dict:
        boot.slack_posts.append((channel, text))
        return {"ok": True, "channel": channel, "ts": "1700000000.000100"}

    monkeypatch.setattr(slack_sdk.web.async_client.AsyncWebClient, "chat_postMessage", chat_post_message)

    def scripted_stream(self: Any, **kwargs: Any) -> Any:
        """Claude scripted at the SDK: the deliver_response answer to the deep research notice the request carries."""
        boot.claude_calls.append(kwargs)
        asked = _last_message_text(kwargs)
        if cancelled_notice(DR_QUERY) in asked:
            reply, summary = DR_CANCELLED_REPLY, DR_CANCELLED_SUMMARY
        elif research_failed_notice(DR_QUERY) in asked:
            reply, summary = DR_FAILED_REPLY, DR_FAILED_SUMMARY
        else:
            reply, summary = "UNSCRIPTED REQUEST", "unscripted"
        body = {"full_response": reply, "response_summary": summary, "rich_content": None}
        message = AnthropicMessage(
            id="msg_scripted", type="message", role="assistant", model=kwargs["model"], stop_reason="tool_use",
            content=[ToolUseBlock(type="tool_use", id="toolu_scripted", name="deliver_response",
                                  input={"response": json.dumps(body)})],
            usage=Usage(input_tokens=10, output_tokens=5, cache_creation_input_tokens=0, cache_read_input_tokens=0))

        class Stream:
            async def __aenter__(self) -> "Stream":
                return self

            async def __aexit__(self, *exc: Any) -> None:
                return None

            def __aiter__(self) -> "Stream":
                return self

            async def __anext__(self) -> Any:
                raise StopAsyncIteration

            async def get_final_message(self) -> AnthropicMessage:
                return message

        return Stream()

    monkeypatch.setattr(anthropic.resources.messages.messages.AsyncMessages, "stream", scripted_stream)

    async def serve(asgi_app: Any, config: Any) -> None:
        # After the graph is built: installed earlier, the aiohttp fake breaks google.genai while it is constructed.
        fake = http_boundary_fake.install(monkeypatch)
        fake.todo_empty_account()
        fake.gmail_empty()
        boot.http = fake
        try:
            await _probe(apps[-1], asgi_app, boot, caplog)
        except BaseException as exc:  # main() swallows what the served task raises
            boot.probe_error = exc

    monkeypatch.setattr("hypercorn.asyncio.serve", serve)
    caplog.set_level(logging.DEBUG)
    await main.main()
    boot.shutdown_logged = any("Graceful shutdown complete" in r.getMessage() for r in caplog.records)
    boot.recorded_names = sorted({name for name, _, _, _ in boot.calls})
    assert services.fallbacks == [], f"Secret Manager was asked for {services.fallbacks}"
    return boot


# ----------------------------------------------------------------------------- the checks

def _check_startup(boot: Boot) -> List[str]:
    problems = []
    for record in boot.startup:
        message = record.getMessage()
        if "❌" in message or record.levelno >= logging.ERROR:
            problems.append(f"startup error line: {message[:160]}")
        if STARTUP_LINE.search(message) and not any(key in message for key in REASONED_STARTUP_LINES):
            problems.append(f"startup line says a feature is off: {message[:160]}")
    if not boot.startup:
        problems.append("no startup records captured")
    if not boot.shutdown_logged:
        problems.append("main() did not reach 'Graceful shutdown complete'")
    return problems


def _check_served_app(boot: Boot) -> List[str]:
    problems = []
    if boot.served_is_quart:
        problems.append("serve() got the Quart app: the MCP dispatcher is not in front of it")
    status, body = boot.mcp_discovery or (None, None)
    if status != 200 or not isinstance(body, dict) or "authorization_endpoint" not in body:
        problems.append(f"an MCP path did not reach the SDK app: {status} {body}")
    if boot.health_through_served != 200:
        problems.append(f"a non-MCP path did not reach the Quart app through the dispatcher: {boot.health_through_served}")
    return problems


def _check_agent_task_queue(boot: Boot) -> List[str]:
    path = "projects/p9-test-project/locations/us-central1/queues/agent-tasks-dev"
    asked = [calls for calls, _ in boot.queue_calls if path in calls]
    created = [created for _, created in boot.queue_calls if path in created]
    problems = []
    if not asked:
        problems.append(f"no Cloud Tasks client asked for {path}")
    if not created:
        problems.append(f"the missing queue {path} was not created")
    return problems


def _write_url_map(rules: List[str]) -> None:
    """Rewrite the url_map pin: the header comment lines are kept, the rules follow sorted (duplicates kept)."""
    header = [line for line in URL_MAP.read_text().splitlines() if line.startswith("#")]
    URL_MAP.write_text("\n".join(header + sorted(rules)) + "\n")


def _update_pins_if_asked(boot: Boot) -> None:
    """Under UPDATE_PINS=1 only; called by the boot test, never by a check (the self-tests feed checks invented rules)."""
    if os.environ.get("UPDATE_PINS") == "1":
        _write_url_map(boot.rules)


def _check_url_map(boot: Boot) -> List[str]:
    expected = [line for line in URL_MAP.read_text().splitlines() if line and not line.startswith("#")]
    missing = Counter(expected) - Counter(boot.rules)
    extra = Counter(boot.rules) - Counter(expected)
    problems = [f"route not served: {rule} x{n}" for rule, n in sorted(missing.items())]
    problems += [f"route served but not pinned: {rule} x{n}" for rule, n in sorted(extra.items())]
    if sorted(expected) != expected:
        problems.append("the pinned url_map file is not sorted")
    return problems


def _optional(annotation: Any) -> bool:
    return typing.get_origin(annotation) in (typing.Union, types.UnionType) and type(None) in typing.get_args(annotation)


def _check_blueprint_calls(boot: Boot) -> List[str]:
    problems = []
    asked = _main_blueprint_calls()
    recorded = set(boot.recorded_names)
    problems += [f"main.py calls {n} but the boot never did" for n in sorted(asked - recorded)]
    problems += [f"the boot recorded {n} but main.py does not call it" for n in sorted(recorded - asked)]
    for name, original, args, kwargs in boot.calls:
        signature = inspect.signature(original)
        try:
            bound = signature.bind(*args, **kwargs)
        except TypeError as exc:
            problems.append(f"{name}: arguments do not bind: {exc}")
            continue
        bound.apply_defaults()
        try:
            hints = typing.get_type_hints(original)
        except Exception:
            hints = {}
        for param in signature.parameters.values():
            annotation = hints.get(param.name, param.annotation)
            if (_optional(annotation) or param.default is None) and bound.arguments.get(param.name) is None:
                problems.append(f"{name}: Optional parameter {param.name} is None in the deployed wiring")
    return problems


def _factory_of(boot: Boot) -> Any:
    [provider] = [kw["lelik_agent_provider"] for name, _, _, kw in boot.calls if name == "create_voice_webhook_blueprint"]
    factory = getattr(provider, "__self__", None)
    assert factory is not None, "lelik_agent_provider is not a bound method: the agent factory cannot be reached"
    return factory


def _check_cabinet_repository(boot: Boot) -> List[str]:
    [cabinet] = [kw for name, _, _, kw in boot.calls if name == "create_user_cabinet_blueprint"]
    factory = _factory_of(boot)
    if cabinet["fact_repo"] is None or factory.repository is None:
        return ["the Cabinet's fact_repo or the agent factory's repository is None (#148)"]
    if cabinet["fact_repo"] is not factory.repository:
        return ["the Cabinet's fact_repo is not the agent factory's repository (#148)"]
    return []


def _check_seed(boot: Boot) -> List[str]:
    account, user = boot.seeded or (None, None)
    problems = []
    if account is None or account.account_id != "a1":
        problems.append("account a1 was not stored by the recorded repository")
    if user is None or user.user_id != "u1" or user.account_id != "a1":
        problems.append("user u1 was not stored by the recorded repository")
    return problems


def _check_matrix(boot: Boot) -> List[str]:
    problems = []
    sent = set(boot.rows)
    pinned = set(MATRIX)
    problems += [f"task type sent but not pinned: {t}" for t in sorted(sent - pinned)]
    problems += [f"task type pinned but not sent: {t}" for t in sorted(pinned - sent)]
    branches = worker_triggers.dispatch_branches()
    problems += [f"dispatch branch with no matrix row: {t}" for t in sorted(branches - sent)]
    for task_type, row in sorted(boot.rows.items()):
        text = json.dumps(row.body) if row.body is not None else ""
        if row.status in REFUSAL_STATUSES or REFUSAL_BODY.search(text) or row.body == {"status": "disabled"}:
            problems.append(f"{task_type}: a configuration refusal ({row.status} {row.body})")
        expect = MATRIX.get(task_type)
        if expect is None:
            continue
        if (row.status, row.body) != (expect.status, expect.body):
            problems.append(f"{task_type}: answered {row.status} {row.body}, pinned {expect.status} {expect.body}")
        if expect.log and not any(expect.log in r.getMessage() for r in row.records):
            problems.append(f"{task_type}: no log record contains {expect.log!r}")
        if expect.exception and not any(r.exc_info and expect.exception in repr(r.exc_info[1]) for r in row.records):
            problems.append(f"{task_type}: no record carries {expect.exception}")
    video = boot.rows.get("video_generation_polling")
    if video is not None and any(error is None for _, error in video.connects):
        problems.append("the video poll made a real connection")
    if boot.unknown != (400, {"error": "unknown task_type"}):
        problems.append(f"an unknown task_type answered {boot.unknown}")
    if boot.anonymous != (401, {"error": "unauthorized"}):
        problems.append(f"an anonymous /worker request answered {boot.anonymous}")
    return problems


def _check_platform_workers(boot: Boot) -> List[str]:
    from src.adapters.platform.factory import PlatformAdapterFactory
    from src.adapters.slack.http_adapter import HTTPModeAdapter
    from src.adapters.telegram.webhook_adapter import TelegramWebhookAdapter

    problems = []
    worker = boot.worker_closure.get("worker_handler")
    if worker is None:
        return ["the /worker route has no worker_handler in its closure"]
    slack, telegram = worker._slack_worker, worker._telegram_worker
    if getattr(slack, "__func__", None) is not HTTPModeAdapter.handle_queued_event:
        problems.append("the attached Slack worker is not HTTPModeAdapter.handle_queued_event")
    elif getattr(slack.__self__.start, "__name__", "") != "start_shared_app":
        problems.append("the attached Slack adapter does not serve the shared app (its start is not start_shared_app)")
    if getattr(telegram, "__func__", None) is not TelegramWebhookAdapter.handle_queued_update:
        problems.append("the attached Telegram worker is not TelegramWebhookAdapter.handle_queued_update")
    elif PlatformAdapterFactory._adapters.get("telegram") is not telegram.__self__:
        problems.append("the registered Telegram adapter is not the one the worker is attached to")
    return problems


def _last_message_text(call: dict) -> str:
    content = call["messages"][-1]["content"]
    if isinstance(content, str):
        return content
    return " ".join(block.get("text", "") for block in content if isinstance(block, dict))


def _check_seeded_rows(boot: Boot) -> List[str]:
    """The user-visible outcome of the five seeded rows: what crossed each boundary and what was stored."""
    problems = []
    if boot.http is None:
        problems.append("no HTTP fake")
    else:
        problems += [f"unrouted HTTP request: {r.method} {r.url}" for r in boot.http.unrouted]
        seen = Counter((r.method, r.url.split("?")[0]) for r in boot.http.requests)
        if seen != SEEDED_HTTP:
            problems.append(f"HTTP requests differ: missing {dict(SEEDED_HTTP - seen)}, extra {dict(seen - SEEDED_HTTP)}")
    config = boot.todo_config
    subscriptions = [(s.sub_id, s.list_id) for s in config.subscriptions] if config is not None else None
    if config is None or config.primary_list_id != "l1" or subscriptions != [("s1", "l1")]:
        problems.append(f"To Do config: primary list {getattr(config, 'primary_list_id', None)!r}, subscriptions {subscriptions}")
    reindex = {"task_type": "reindex_task_list", "user_id": "u1", "list_id": "l1"}
    renew = {"task_type": "renew_task_subscriptions", "user_id": "u1"}
    for body in (reindex, renew):
        if boot.task_bodies.count(body) != 1:
            problems.append(f"{boot.task_bodies.count(body)} Cloud Tasks bodies equal {body}, wanted 1")
    status = getattr(boot.indexing_job, "status", None)
    if status != "completed":
        problems.append(f"the seeded indexing job is {status!r}, not completed")
    if boot.slack_posts != [("C1", DR_FAILED_REPLY), ("C1", DR_CANCELLED_REPLY)]:
        problems.append(f"Slack received {boot.slack_posts}")
    if len(boot.claude_calls) != 2:
        problems.append(f"Claude was called {len(boot.claude_calls)} times, wanted 2")
    else:
        for call, notice in zip(boot.claude_calls, (research_failed_notice(DR_QUERY), cancelled_notice(DR_QUERY))):
            if notice not in _last_message_text(call):
                problems.append(f"a Claude request does not carry {notice!r}")
            if "deliver_response" not in [tool.get("name") for tool in call.get("tools") or []]:
                problems.append("a Claude request does not declare the deliver_response tool")
    expected_history = [
        ("user", research_failed_notice(DR_QUERY), None), ("model", DR_FAILED_SUMMARY, DR_FAILED_REPLY),
        ("user", cancelled_notice(DR_QUERY), None), ("model", DR_CANCELLED_SUMMARY, DR_CANCELLED_REPLY),
    ]
    if boot.history != expected_history:
        problems.append(f"history of u1:C1 is {boot.history}")
    return problems


CHECKS = [
    _check_startup, _check_served_app, _check_agent_task_queue, _check_url_map, _check_blueprint_calls, _check_cabinet_repository,
    _check_seed, _check_matrix, _check_platform_workers, _check_seeded_rows,
]


@pytest.mark.slow
async def test_main_boots_offline_and_serves_the_deployed_surface(monkeypatch, caplog):
    boot = await _boot(monkeypatch, caplog)
    if boot.probe_error is not None:
        raise boot.probe_error
    _update_pins_if_asked(boot)
    problems = [f"{check.__name__}: {p}" for check in CHECKS for p in check(boot)]
    assert not problems, "\n".join(problems)


# ----------------------------------------------------------------------------- the checks' own tests

def _record(message: str, level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord("root", level, __file__, 1, message, None, None)


def _factory(a: Optional[int] = None, b: int = 0):
    return None


def test_every_blueprint_factory_main_imports_is_called():
    assert {name for _, name in _main_blueprint_factories()} == _main_blueprint_calls()


def test_a_startup_error_and_a_feature_off_line_are_problems():
    boot = Boot(startup=[_record("✅ fine"), _record("❌ broke"), _record("ℹ️ X disabled (Y not set)")], shutdown_logged=True)
    assert len(_check_startup(boot)) == 2


def test_a_missing_shutdown_line_is_a_problem():
    assert _check_startup(Boot(startup=[_record("fine")])) == ["main() did not reach 'Graceful shutdown complete'"]


def test_the_agent_task_queue_must_be_asked_for_and_created():
    path = "projects/p9-test-project/locations/us-central1/queues/agent-tasks-dev"
    assert _check_agent_task_queue(Boot(queue_calls=[([path], [path])])) == []
    assert len(_check_agent_task_queue(Boot(queue_calls=[([], [])]))) == 2


def test_the_url_map_is_compared_both_ways_and_with_duplicates():
    pinned = [line for line in URL_MAP.read_text().splitlines() if line and not line.startswith("#")]
    assert _check_url_map(Boot(rules=list(pinned))) == []
    assert any("not served" in p for p in _check_url_map(Boot(rules=pinned[1:])))
    assert any("not pinned" in p for p in _check_url_map(Boot(rules=pinned + ["GET /new"])))
    assert any("/health x1" in p for p in _check_url_map(Boot(rules=pinned + ["GET /health"])))


def test_an_optional_parameter_left_none_is_a_problem():
    boot = Boot(calls=[("create_x_blueprint", _factory, (), {"b": 1})], recorded_names=["create_x_blueprint"])
    problems = _check_blueprint_calls(boot)
    assert "create_x_blueprint: Optional parameter a is None in the deployed wiring" in problems


def test_a_factory_main_calls_but_the_boot_did_not_record_is_a_problem():
    problems = _check_blueprint_calls(Boot())
    assert any("the boot never did" in p for p in problems)


def test_the_agent_factory_must_be_reachable_from_the_voice_provider():
    class Factory:
        repository = object()

        async def get_lelik(self, user_id):
            return None

    factory = Factory()
    cabinet = ("create_user_cabinet_blueprint", None, (), {"fact_repo": factory.repository})
    voice = ("create_voice_webhook_blueprint", None, (), {"lelik_agent_provider": factory.get_lelik})
    assert _check_cabinet_repository(Boot(calls=[cabinet, voice])) == []
    other = ("create_user_cabinet_blueprint", None, (), {"fact_repo": object()})
    assert len(_check_cabinet_repository(Boot(calls=[other, voice]))) == 1
    unreachable = ("create_voice_webhook_blueprint", None, (), {"lelik_agent_provider": lambda user_id: None})
    with pytest.raises(AssertionError):
        _check_cabinet_repository(Boot(calls=[cabinet, unreachable]))


def _matrix_boot(**overrides: Row) -> Boot:
    rows = {t: Row(e.status, e.body, [], []) for t, e in MATRIX.items() if e.body is not None}
    rows.update({t: Row(500, None, []) for t, e in MATRIX.items() if e.body is None})
    rows.update(overrides)
    return Boot(rows=rows, unknown=(400, {"error": "unknown task_type"}), anonymous=(401, {"error": "unauthorized"}))


def test_a_matrix_answer_that_differs_from_the_pinned_one_is_a_problem():
    boot = _matrix_boot(consolidation=Row(200, {"status": "skipped"}, []))
    assert any(p.startswith("consolidation: answered 200") for p in _check_matrix(boot))


@pytest.mark.parametrize("status, body", [
    (503, {"error": "x"}), (501, {"error": "services not configured"}), (200, {"status": "disabled"}),
    (200, {"error": "retrieval not ready"}),
])
def test_a_configuration_refusal_is_a_problem_whatever_the_pinned_table_says(status, body):
    boot = _matrix_boot(consolidation=Row(status, body, []))
    assert any(p.startswith("consolidation: a configuration refusal") for p in _check_matrix(boot))


def test_a_pinned_cause_must_be_in_the_logs_of_its_row(monkeypatch):
    monkeypatch.setitem(MATRIX, "setup_microsoft_todo", Expect(500, None, exception="ValueError('No MS To Do credentials for user u1')"))
    boot = _matrix_boot()
    problems = _check_matrix(boot)
    assert any(p.startswith("daily_email_review: no log record contains") for p in problems)
    assert any(p.startswith("setup_microsoft_todo: no record carries") for p in problems)


def test_an_unpinned_or_unsent_task_type_is_a_problem():
    boot = _matrix_boot(brand_new=Row(200, {}, []))
    del boot.rows["start_drive_sync"]
    problems = _check_matrix(boot)
    assert "task type sent but not pinned: brand_new" in problems
    assert "task type pinned but not sent: start_drive_sync" in problems


def test_the_matrix_pins_exactly_the_triggers_the_deploy_sends():
    sent = set(worker_triggers.scheduler_task_types()) | set(ENQUEUE_PAYLOADS)
    assert set(MATRIX) == sent == worker_triggers.dispatch_branches()


def test_a_video_poll_that_connected_is_a_problem():
    boot = _matrix_boot(video_generation_polling=Row(200, {"attempt": 1, "status": "retry"}, [], [(("h", 443), None)]))
    assert "the video poll made a real connection" in _check_matrix(boot)


def test_serve_must_get_the_dispatcher_with_the_mcp_app_behind_it():
    ok = Boot(served_is_quart=False, mcp_discovery=(200, {"authorization_endpoint": "x"}), health_through_served=200)
    assert _check_served_app(ok) == []
    assert any("got the Quart app" in p for p in _check_served_app(Boot(
        served_is_quart=True, mcp_discovery=(404, None), health_through_served=200)))
    assert any("MCP path did not reach" in p for p in _check_served_app(Boot(
        served_is_quart=False, mcp_discovery=(404, {"error": "x"}), health_through_served=200)))
    assert any("non-MCP path" in p for p in _check_served_app(Boot(
        served_is_quart=False, mcp_discovery=(200, {"authorization_endpoint": "x"}), health_through_served=404)))


def test_a_none_on_both_sides_of_the_cabinet_identity_is_a_problem():
    class Factory:
        repository = None

        async def get_lelik(self, user_id):
            return None

    factory = Factory()
    cabinet = ("create_user_cabinet_blueprint", None, (), {"fact_repo": None})
    voice = ("create_voice_webhook_blueprint", None, (), {"lelik_agent_provider": factory.get_lelik})
    assert _check_cabinet_repository(Boot(calls=[cabinet, voice])) == [
        "the Cabinet's fact_repo or the agent factory's repository is None (#148)"]


def _pep604(a: int | None, b: int = 0):
    return None


def test_a_pep604_optional_parameter_is_recognised():
    boot = Boot(calls=[("create_x_blueprint", _pep604, (None,), {})], recorded_names=["create_x_blueprint"])
    assert "create_x_blueprint: Optional parameter a is None in the deployed wiring" in _check_blueprint_calls(boot)


def test_the_slack_worker_must_belong_to_an_adapter_that_serves_the_shared_app(monkeypatch):
    from types import MethodType, SimpleNamespace
    from src.adapters.platform.factory import PlatformAdapterFactory
    from src.adapters.slack.http_adapter import HTTPModeAdapter
    from src.adapters.telegram.webhook_adapter import TelegramWebhookAdapter

    def start_shared_app():
        return None

    def worker(start):
        telegram = MethodType(TelegramWebhookAdapter.handle_queued_update, SimpleNamespace())
        monkeypatch.setattr(PlatformAdapterFactory, "_adapters", {"telegram": telegram.__self__})
        handler = SimpleNamespace(
            _slack_worker=MethodType(HTTPModeAdapter.handle_queued_event, SimpleNamespace(start=start)),
            _telegram_worker=telegram)
        return Boot(worker_closure={"worker_handler": handler})

    assert _check_platform_workers(worker(start_shared_app)) == []
    assert any("start_shared_app" in p for p in _check_platform_workers(worker(lambda: None)))


# ----------------------------------------------------------------------------- the seeded rows' own tests

def _seeded_http(extra: Tuple[Tuple[str, str], ...] = (), unrouted: Tuple[Tuple[str, str], ...] = ()) -> Any:
    return SimpleNamespace(
        requests=[SimpleNamespace(method=m, url=u) for (m, u) in [*SEEDED_HTTP.elements(), *extra]],
        unrouted=[SimpleNamespace(method=m, url=u) for (m, u) in unrouted])


def _claude_call(notice: str) -> dict:
    return {"messages": [{"role": "user", "content": [{"type": "text", "text": notice}]}],
            "tools": [{"name": "deliver_response"}]}


_REINDEX = {"task_type": "reindex_task_list", "user_id": "u1", "list_id": "l1"}
_RENEW = {"task_type": "renew_task_subscriptions", "user_id": "u1"}
_HISTORY = [
    ("user", research_failed_notice(DR_QUERY), None), ("model", DR_FAILED_SUMMARY, DR_FAILED_REPLY),
    ("user", cancelled_notice(DR_QUERY), None), ("model", DR_CANCELLED_SUMMARY, DR_CANCELLED_REPLY),
]


def _seeded_boot(**overrides: Any) -> Boot:
    """A Boot holding exactly what `_check_seeded_rows` expects; an override breaks one item."""
    fields: Dict[str, Any] = dict(
        http=_seeded_http(),
        todo_config=SimpleNamespace(primary_list_id="l1", subscriptions=[SimpleNamespace(sub_id="s1", list_id="l1")]),
        task_bodies=[dict(_REINDEX), dict(_RENEW)],
        indexing_job=SimpleNamespace(status="completed"),
        slack_posts=[("C1", DR_FAILED_REPLY), ("C1", DR_CANCELLED_REPLY)],
        claude_calls=[_claude_call(research_failed_notice(DR_QUERY)), _claude_call(cancelled_notice(DR_QUERY))],
        history=list(_HISTORY),
    )
    fields.update(overrides)
    return Boot(**fields)


def test_the_seeded_rows_pass_with_their_outcomes():
    assert _check_seeded_rows(_seeded_boot()) == []


@pytest.mark.parametrize("overrides", [
    pytest.param(dict(http=None), id="no-http-fake"),
    pytest.param(dict(http=_seeded_http(unrouted=(("GET", "https://example.test/x"),))), id="unrouted-request"),
    pytest.param(dict(http=_seeded_http(extra=(("GET", f"{_GMAIL_URL}/messages"),))), id="extra-get"),
    pytest.param(dict(todo_config=SimpleNamespace(primary_list_id="l1", subscriptions=[
        SimpleNamespace(sub_id="s1", list_id="l1"), SimpleNamespace(sub_id="s2", list_id="l1")])), id="second-subscription"),
    pytest.param(dict(task_bodies=[dict(_REINDEX), dict(_REINDEX), dict(_RENEW)]), id="second-reindex-body"),
    pytest.param(dict(indexing_job=SimpleNamespace(status="running")), id="job-running"),
    pytest.param(dict(slack_posts=[("C1", DR_FAILED_REPLY)]), id="slack-without-cancelled-reply"),
    pytest.param(dict(claude_calls=[_claude_call(research_failed_notice(DR_QUERY))]), id="one-claude-call"),
    pytest.param(dict(history=list(_HISTORY[:2])), id="history-without-second-pair"),
])
def test_a_seeded_row_outcome_that_is_missing_is_a_problem(overrides):
    assert _check_seeded_rows(_seeded_boot(**overrides))


def test_a_pinned_exception_found_in_a_row_record_is_not_a_problem(monkeypatch):
    cause = "No MS To Do credentials for user u1"
    monkeypatch.setitem(MATRIX, "setup_microsoft_todo", Expect(500, None, exception=f"ValueError('{cause}')"))
    record = logging.LogRecord("root", logging.ERROR, __file__, 1, "setup failed", None, (ValueError, ValueError(cause), None))
    problems = _check_matrix(_matrix_boot(setup_microsoft_todo=Row(500, None, [record])))
    assert not [p for p in problems if p.startswith("setup_microsoft_todo:")]
