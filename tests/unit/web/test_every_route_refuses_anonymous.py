"""Every route of every deployed web app refuses an anonymous request (P7, B1: the runtime probe).

Each blueprint factory main.py, indexer_main.py and sandbox_main.py register is built on a fresh Quart app with its
real gate (the session service, the OIDC gate over a non-empty identity set, the Twilio verifier, the Slack and
Telegram secrets, the sandbox caller gate), and everything behind the gate is a `Tripwire` or a real repository on
the evaluating Firestore fake. The probe walks `url_map.iter_rules()` and sends every (rule, method) outside `PUBLIC`
with no credentials, then asserts the app's refusal AND that nothing behind the gate was touched or written. A new
route is probed the day it is added; a new blueprint fails the static check below until the probe builds it.

The MCP SDK sub-app main.py dispatches to is probed the same way over ASGI; its dispatched paths must equal the
literals in main.py's `_is_mcp_path`.
"""
import ast
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Dict, List, Optional, Tuple
from unittest.mock import MagicMock
from urllib.parse import urlsplit

import httpx
import pytest
from quart import Quart

from src.adapters.firestore_short_link_repository import FirestoreShortLinkRepository
from src.adapters.slack.http_adapter import HTTPModeAdapter
from src.adapters.telegram.webhook_adapter import TelegramWebhookAdapter
from src.composition.mcp_setup import build_mcp_components
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from src.domain import sandbox_contract as sc
from src.domain.sandbox_contract import SandboxLimits, SandboxOp
from src.services.file_access_token_service import FileAccessTokenService
from src.services.short_link_service import ShortLinkService
from src.web import user_cabinet_app
from src.web.deep_research_webhooks import create_deep_research_webhooks_blueprint
from src.web.file_access_app import create_file_access_blueprint
from src.web.indexer_worker_app import create_indexer_worker_blueprint, worker_oidc_gate
from src.web.landing_app import create_landing_blueprint
from src.web.mcp_consent_app import build_mcp_consent_blueprint
from src.web.microsoft_tasks_webhook import create_microsoft_tasks_webhook_blueprint
from src.web.oauth_app import create_oauth_blueprint
from src.web.sandbox_app import create_sandbox_blueprint
from src.web.sandbox_caller_gate import SandboxCallerGate
from src.web.sandbox_unconfigured_app import create_unconfigured_blueprint
from src.web.short_link_app import create_short_link_blueprint
from src.web.twilio_signature_verifier import verify_twilio_signature
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from src.web.voice_control_plane_app import create_voice_control_plane_blueprint
from src.web.voice_web_call_app import create_voice_web_call_blueprint
from src.web.voice_webhook_app import create_voice_webhook_blueprint
from src.web.worker_oidc_verifier import build_oidc_gate
from tests.support.firestore_eval import EvalDb
from tests.unit.web.gate_fakes import SESSION_SECRET, Tripwire, session_service

ROOT = Path(__file__).resolve().parents[3]
SA = frozenset({"caller@example.test"})


class Probe:
    """What one probed request may disturb: the tripwires it must not touch and the databases it must not write."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.touched: List[str] = []
        self.dbs: List[EvalDb] = []

    def wire(self, name: str) -> Tripwire:
        return Tripwire(name, self.touched)

    def db(self) -> EvalDb:
        db = EvalDb()
        self.dbs.append(db)
        return db

    def writes(self) -> List[str]:
        return [f"{name}/{doc}" for db in self.dbs for name, col in db.cols.items() for doc in col.docs]


# --------------------------------------------------------------------------- the factories, as deployed

def _cabinet(p: Probe):
    w = p.wire
    return create_user_cabinet_blueprint(
        invite_service=w("invite_service"), session_service=session_service(), user_repo=w("user_repo"),
        fact_repo=w("fact_repo"), embedding_service=w("embedding_service"),
        oauth_credentials_port=w("oauth_credentials_port"), gmail_oauth_service=w("gmail_oauth_service"),
        google_tasks_oauth_service=w("google_tasks_oauth_service"), indexed_email_repo=w("indexed_email_repo"),
        email_indexing_service=w("email_indexing_service"), email_job_repo=w("email_job_repo"),
        task_queue=w("task_queue"), task_setup=w("task_setup"), tasks_provider=w("tasks_provider"),
        language_service=w("language_service"), agent_note_port=w("agent_note_port"),
        recurrence_port=w("recurrence_port"), twilio_verify_client=w("twilio_verify_client"),
        user_drive=w("user_drive"), drive_search_service=w("drive_search_service"),
        drive_task_queue=w("drive_task_queue"), drive_transcribe_switch_on=True,
        cabinet_facts=w("cabinet_facts"),   # reviewer edit, facts S4 T2c: main.py passes it (the route delegates to it)
        # reviewer edit, facts S4 T11a/T11b: main.py passes all three (the review queue, the answers, the button labels)
        fact_review_store=w("fact_review_store"), fact_answer_service=w("fact_answer_service"),
        localization=w("localization"),
    ), {}


def _oauth(p: Probe):
    w = p.wire
    return create_oauth_blueprint(
        auth_service=w("auth_service"), session_service=session_service(), auth_registry=w("auth_registry"),
        auth_config=SimpleNamespace(oauth_redirect_uri="https://app.example/auth/callback",
                                    gmail_oauth_redirect_uri="https://app.example/g",
                                    google_tasks_oauth_redirect_uri="https://app.example/t",
                                    access_token_ttl=86400, refresh_token_ttl=86400),
        invite_service=w("invite_service"), gmail_oauth_service=w("gmail_oauth_service"),
        oauth_credentials_port=w("oauth_credentials_port"), google_tasks_oauth_service=w("google_tasks_oauth_service"),
        ms_todo_client_id="ms-client", ms_todo_client_secret="ms-secret",
        ms_todo_redirect_uri="https://app.example/m", onedrive_redirect_uri="https://app.example/o",
        task_queue=w("task_queue"),
    ), {}


def _deep_research(p: Probe):
    return create_deep_research_webhooks_blueprint(
        webhook_secret="whsec_cDctcHJvYmUtc2VjcmV0LWtleS0zMmJ5dGVzLWxvbmc=", task_queue=p.wire("task_queue"),
        fetch_response=p.wire("openai").retrieve, dedup_store=p.wire("dedup_store")), {}


def _ms_tasks(p: Probe):
    return create_microsoft_tasks_webhook_blueprint(
        task_indexing=p.wire("task_indexing"), task_setup=p.wire("task_setup"), webhook_secret="p7-client-state"), {}


def _file_access(p: Probe):
    return create_file_access_blueprint(token_service=FileAccessTokenService(SESSION_SECRET),
                                        media_storage=p.wire("media_storage"), session_service=session_service()), {}


def _short_link(p: Probe):
    repo = FirestoreShortLinkRepository(p.db(), SimpleNamespace(firestore_collection_prefix="p7_"))
    return create_short_link_blueprint(short_links=ShortLinkService(repo, "https://app.example")), {}


def _voice_control_plane(p: Probe):
    w = p.wire
    return create_voice_control_plane_blueprint(
        ephemeral_store=w("ephemeral_store"), quota_service=w("quota_service"),
        prompt_content_store=w("prompt_content_store"), summary_consumer=w("summary").consume,
        oidc_verifier=build_oidc_gate(SA, on_cloud_run=True, label="P7 probe"), alert_sink=w("alert_sink"),
        lelik_agent_provider=w("agent_factory").get_lelik, late_answer_sink=w("notifications").post,
        relay_warmup=w("relay").warm, digest_key=b"k" * 32
    ), {}


def _voice_webhook(p: Probe):
    async def twilio(url, form_params, signature):     # main.py's wrapper with TWILIO_AUTH_TOKEN set
        return verify_twilio_signature("p7-twilio-auth-token-0123456789", url, form_params, signature)

    w = p.wire
    return create_voice_webhook_blueprint(
        user_repository=w("user_repository"), ephemeral_store=w("ephemeral_store"), alert_sink=w("alert_sink"),
        notification_service=w("notification_service"), lelik_agent_provider=w("agent_factory").get_lelik,
        answer_url="https://app.example/voice/answer", signature_verifier=twilio,
        relay_stream_url="wss://relay.example/stream", cipher=AesGcmTestCipher(), digest_key=b"k" * 32
    ), {}


def _voice_web_call(p: Probe):
    w = p.wire
    return create_voice_web_call_blueprint(session_service=session_service(), call_setup=w("call_setup"),
                                           media_room=w("media_room"), ephemeral_store=w("ephemeral_store"),
                                           relay_base_url="wss://relay.example", cipher=AesGcmTestCipher(), digest_key=b"k" * 32), {}


def _landing(p: Probe):
    return create_landing_blueprint(session_service()), {}


def _mcp_consent(p: Probe):
    return build_mcp_consent_blueprint(mcp_service=p.wire("mcp_service"), session_service=session_service()), {}


def _slack(p: Probe):
    w = p.wire
    adapter = HTTPModeAdapter(app=w("bolt_app"), config={"SLACK_SIGNING_SECRET": "p7-signing", "SLACK_BOT_TOKEN": "x"},
                              task_service=w("task_queue"), session_store=w("session_store"),
                              conversation_handler=w("conversation_handler"), iam_service=w("iam_service"),
                              worker_queue=w("worker_queue"))
    return adapter.get_blueprint(), {"url_prefix": "/slack"}


def _telegram(p: Probe):
    w = p.wire
    adapter = TelegramWebhookAdapter(token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11", webhook_secret="p7-tg-secret",
                                     dedup_store=w("dedup_store"), session_store=w("session_store"),
                                     conversation_handler=w("conversation_handler"), iam_service=w("iam_service"),
                                     task_queue=w("task_queue"))
    return adapter.get_blueprint(), {"url_prefix": "/telegram"}


def _indexer(p: Probe):
    gate = worker_oidc_gate(SA, ("https://indexer.example", "https://indexer.example/worker"), on_cloud_run=True)
    return create_indexer_worker_blueprint(p.wire("drive_indexing"), gate), {}


def _sandbox(p: Probe):
    work_root = p.tmp_path / "work"
    work_root.mkdir(exist_ok=True)
    gate = SandboxCallerGate({"render": SA}, {"render": frozenset({"render"})}, "https://sandbox.example")
    ceiling = SandboxLimits(wall_s=sc.CEILING_WALL_S, memory_bytes=sc.CEILING_MEMORY_BYTES, cpu_s=sc.CEILING_CPU_S,
                            file_bytes=sc.CEILING_FILE_BYTES)
    return create_sandbox_blueprint({SandboxOp.RENDER_DOCX: p.wire("engine")}, gate, p.wire("recycler"), ceiling,
                                    {"work_root": str(work_root), "engine_user": None, "recycle": "always"}), {}


def _sandbox_unconfigured(p: Probe):
    return create_unconfigured_blueprint(), {}


# The key is how main.py / indexer_main.py / sandbox_main.py name the factory (see the static check below).
FACTORIES: Dict[str, Callable[[Probe], tuple]] = {
    "create_user_cabinet_blueprint": _cabinet,
    "create_oauth_blueprint": _oauth,
    "create_deep_research_webhooks_blueprint": _deep_research,
    "create_microsoft_tasks_webhook_blueprint": _ms_tasks,
    "create_file_access_blueprint": _file_access,
    "create_short_link_blueprint": _short_link,
    "create_voice_control_plane_blueprint": _voice_control_plane,
    "create_voice_webhook_blueprint": _voice_webhook,
    "create_voice_web_call_blueprint": _voice_web_call,
    "create_landing_blueprint": _landing,
    "build_mcp_consent_blueprint": _mcp_consent,
    "slack_adapter.get_blueprint": _slack,
    "telegram_adapter.get_blueprint": _telegram,
    "create_indexer_worker_blueprint": _indexer,
    "create_sandbox_blueprint": _sandbox,
    "create_unconfigured_blueprint": _sandbox_unconfigured,
}

# Public by design: never probed. One reason each. Every entry must still exist (checked).
PUBLIC: Dict[Tuple[str, str, str], str] = {
    ("create_landing_blueprint", "GET", "/"): "the public landing page (a valid session only redirects to /cabinet)",
    ("create_landing_blueprint", "GET", "/og.png"): "share image for link previews",
    ("create_landing_blueprint", "GET", "/robots.txt"): "crawler policy",
    ("create_user_cabinet_blueprint", "GET", "/health"): "health check, no data",
    ("create_user_cabinet_blueprint", "GET", "/cabinet"): "static Cabinet shell; every datum comes from the gated API",
    ("create_voice_web_call_blueprint", "GET", "/cabinet/call"): "static call page; the call API is gated",
    ("create_voice_web_call_blueprint", "GET", "/cabinet/call/manifest.webmanifest"):
        "Home Screen install: iOS fetches it without the session cookie",
    ("create_voice_web_call_blueprint", "GET", "/cabinet/call/icon-<int:size>.png"):
        "Home Screen icon, fetched without the cookie; sizes are an allowlist (test_voice_web_call_install)",
    ("create_oauth_blueprint", "GET", "/auth/login"): "starts sign-in: only redirects to the provider",
    ("create_oauth_blueprint", "POST", "/auth/logout"): "only clears the caller's own cookies",
    ("create_indexer_worker_blueprint", "GET", "/health"): "health check, no data",
    ("create_sandbox_blueprint", "GET", "/health"): "health check, no data",
    ("create_unconfigured_blueprint", "GET", "/health"): "health check, no data",
}

_GRAPH_NOTIFICATION = {"value": [{"subscriptionId": "sub-1", "changeType": "updated",
                                  "resource": "me/todo/lists/L1/tasks/T1"}]}
_SVIX_EVENT = {"type": "response.completed", "data": {"id": "resp_1", "metadata": {"user_id": "u1"}}}
_SLACK_EVENT = {"type": "event_callback", "event_id": "Ev1",
                "event": {"type": "message", "channel": "C1", "user": "U1", "text": "hi", "ts": "1.1"}}
# A well-formed button press (Facts S4 T5d), so an unsigned one is refused by the signature, not by a missing field.
_SLACK_PRESS = {"type": "block_actions", "user": {"id": "U1"}, "channel": {"id": "C1"}, "message": {"ts": "1.1"},
                "actions": [{"block_id": "fact_question:q1", "action_id": "still_true", "action_ts": "1.2"}]}
_TELEGRAM_UPDATE = {"update_id": 1, "message": {"message_id": 1, "date": 0, "text": "hi",
                                                "chat": {"id": 7, "type": "private"},
                                                "from": {"id": 7, "is_bot": False, "first_name": "x"}}}

# What an anonymous caller sends, where the default (no body for GET, `{"probe": true}` otherwise) would be refused
# for the wrong reason (missing input) instead of by the gate. Keyed by (method, rule).
INPUT: Dict[Tuple[str, str], dict] = {
    ("POST", "/webhook/microsoft-tasks/<user_id>"): {"json": _GRAPH_NOTIFICATION},
    ("POST", "/webhooks/openai/deep-research"): {"json": _SVIX_EVENT},
    ("POST", "/slack/events"): {"json": _SLACK_EVENT},
    ("POST", "/slack/interactivity"): {"form": {"payload": json.dumps(_SLACK_PRESS)}},
    ("POST", "/telegram/webhook"): {"json": _TELEGRAM_UPDATE},
    ("GET", "/mcp/consent"): {"query": {"req": "consent-jwt"}},
    ("POST", "/mcp/consent"): {"form": {"req": "consent-jwt", "action": "approve"}},
    ("GET", "/auth/callback"): {"query": {"code": "c", "state": "s"}},
    ("GET", "/auth/connect-gmail/callback"): {"query": {"code": "c", "state": "s"}},
    ("GET", "/auth/connect-google-tasks/callback"): {"query": {"code": "c", "state": "s"}},
    ("GET", "/auth/connect-microsoft-todo/callback"): {"query": {"code": "c", "state": "s"}},
    ("GET", "/auth/connect-onedrive/callback"): {"query": {"code": "c", "state": "s"}},
    ("GET", "/join"): {"query": {"code": "INV-123"}},
    ("POST", "/v1/<op>"): {"path": {"op": "render.docx"}, "data": b"payload"},
    ("GET", "/s/<code>"): {"path": {"code": "abcdefghij"}},
    # A well-formed batch (Facts S4 T11b), so an anonymous one is refused by the gate, not by the body check.
    ("POST", "/api/user/facts/review-answers"): {"json": {"answers": [{"question_id": "q1", "answer": "still_true"}]}},
}


def _login_redirect(resp) -> bool:
    return resp.status_code in (302, 303, 307) and urlsplit(resp.headers.get("Location", "")).path == "/auth/login"


def _default_refusal(resp) -> bool:
    return resp.status_code in (401, 403, 404) or _login_redirect(resp)


def _state_error_redirect(resp) -> bool:
    location = resp.headers.get("Location", "")
    return resp.status_code == 302 and location.startswith("/cabinet?") and location.endswith("_error=state")


# Refusals that are not 401/403/404/login. One reason each.
REFUSAL: Dict[Tuple[str, str], Tuple[Callable, str]] = {
    ("POST", "/webhook/microsoft-tasks/<user_id>"): (
        lambda r: r.status_code == 202, "Graph requires 202 for every notification; one without the clientState is "
                                        "skipped, which the side-effect check holds"),
    ("GET", "/auth/callback"): (lambda r: r.status_code == 400, "no state cookie: the CSRF check answers 400"),
    ("GET", "/auth/connect-gmail/callback"): (_state_error_redirect, "no state cookie: back to /cabinet?…_error=state"),
    ("GET", "/auth/connect-google-tasks/callback"): (_state_error_redirect, "same"),
    ("GET", "/auth/connect-microsoft-todo/callback"): (_state_error_redirect, "same"),
    ("GET", "/auth/connect-onedrive/callback"): (_state_error_redirect, "same"),
}
# A whole factory whose refusal is not 401/403/404/login.
FACTORY_REFUSAL: Dict[str, Tuple[Callable, str]] = {
    "create_unconfigured_blueprint": (lambda r: r.status_code == 503,
                                      "sandbox before its URL is known: every route but /health is 503 for everyone"),
}
_ARG = re.compile(r"<(?:(\w+)(?:\([^)]*\))?:)?(\w+)>")


def _url(rule: str, values: dict) -> str:
    def fill(m: "re.Match") -> str:
        converter, name = m.group(1), m.group(2)
        if name in values:
            return str(values[name])
        return {"int": "192", "path": "index.html"}.get(converter or "", "probe")
    return _ARG.sub(fill, rule)


def _build(key: str, tmp_path: Path) -> Tuple[Quart, Probe]:
    probe = Probe(tmp_path)
    blueprint, options = FACTORIES[key](probe)
    app = Quart(f"probe_{key}")
    app.register_blueprint(blueprint, **options)
    return app, probe


async def _probe_one(key: str, app: Quart, probe: Probe, method: str, rule: str) -> Optional[str]:
    """Send one anonymous request; return a failure description or None."""
    probe.touched.clear()           # construction and earlier requests do not count, only this request
    spec = INPUT.get((method, rule), {})
    kwargs: dict = {"query_string": spec.get("query")}
    if method != "GET":
        if "form" in spec:
            kwargs["form"] = spec["form"]
        elif "data" in spec:
            kwargs["data"] = spec["data"]
        else:
            kwargs["json"] = spec.get("json", {"probe": True})
    resp = await app.test_client().open(_url(rule, spec.get("path", {})), method=method, **kwargs)

    check, _ = FACTORY_REFUSAL.get(key) or REFUSAL.get((method, rule), (_default_refusal, ""))
    problems = []
    if not check(resp):
        problems.append(f"answered {resp.status_code} {resp.headers.get('Location', '')}".rstrip())
    if probe.touched:
        problems.append(f"touched {probe.touched}")
    if probe.writes():
        problems.append(f"wrote {probe.writes()}")
    return f"{method} {rule}: " + "; ".join(problems) if problems else None


def _rules(app: Quart) -> List[Tuple[str, str]]:
    return sorted((method, rule.rule) for rule in app.url_map.iter_rules() if rule.endpoint != "static"
                  for method in rule.methods - {"HEAD", "OPTIONS"})


@pytest.mark.parametrize("key", sorted(FACTORIES))
async def test_every_route_refuses_an_anonymous_caller_and_changes_nothing(key, tmp_path, monkeypatch):
    monkeypatch.setattr(user_cabinet_app, "DOCS_OWNER_USER_ID", "owner-user")  # the owner gate is live, not off
    app, probe = _build(key, tmp_path)
    routes = _rules(app)
    assert routes, f"{key} built no routes"
    failures = []
    for method, rule in routes:
        if (key, method, rule) in PUBLIC:
            continue
        failure = await _probe_one(key, app, probe, method, rule)
        if failure:
            failures.append(failure)
    assert failures == []


def test_every_public_entry_names_a_live_route(tmp_path):
    live = {key: _rules(_build(key, tmp_path)[0]) for key in {entry[0] for entry in PUBLIC}}
    stale = [entry for entry in PUBLIC if (entry[1], entry[2]) not in live[entry[0]]]
    assert stale == []


# --------------------------------------------------------------------------- the MCP SDK sub-app

# main.py's `_is_mcp_path` literals (checked below). The protected-resource entry is a prefix.
MCP_DISPATCHED = {"/mcp", "/mcp/", "/authorize", "/token", "/register", "/revoke",
                  "/.well-known/oauth-authorization-server", "/.well-known/oauth-protected-resource"}
MCP_PREFIXES = {"/.well-known/oauth-protected-resource"}
MCP_PUBLIC: Dict[str, str] = {
    "/.well-known/oauth-authorization-server": "RFC 8414 authorization-server metadata",
    "/.well-known/oauth-protected-resource/mcp": "RFC 9728 protected-resource metadata",
}
# OAuth endpoints answer a bad anonymous request with the RFC 6749 §5.2 errors (400/401), not a login.
_MCP_REFUSED = {400, 401, 404}
_MCP_INPUT = {
    "/register": {"json": {"redirect_uris": ["https://evil.example/cb"]}},   # anonymous DCR: the host allowlist refuses
    "/authorize": {"params": {"client_id": "unknown", "response_type": "code", "code_challenge": "c" * 43,
                              "redirect_uri": "https://claude.ai/cb"}},
    "/token": {"data": {"grant_type": "refresh_token", "refresh_token": "r"}},
}


def _mcp_world():
    db = EvalDb()
    components = build_mcp_components(
        db_client=db, env_config=SimpleNamespace(mcp_oauth_clients_collection="c", mcp_auth_codes_collection="a",
                                                 mcp_refresh_tokens_collection="r"),
        auth_config=SimpleNamespace(mcp_resource_uri="https://mcp.example.test/mcp", oauth_session_secret=SESSION_SECRET,
                                    mcp_access_token_ttl=3600, mcp_refresh_token_ttl=2592000, mcp_auth_code_ttl=600,
                                    mcp_consent_request_ttl=600),
        search_enrichment_service=MagicMock(side_effect=AssertionError("search reached anonymously")),
        cipher=AesGcmTestCipher(), digest_key=bytes(range(32)))
    return db, components.fastmcp.streamable_http_app()


def _mcp_routes(asgi) -> List[Tuple[str, str]]:
    out = []
    for route in asgi.routes:
        methods = route.methods or {"GET", "POST", "DELETE"}    # the /mcp endpoint is a raw ASGI mount: all three
        out += [(m, route.path) for m in sorted(methods - {"HEAD", "OPTIONS"})]
    return out


def _dispatched(path: str) -> bool:
    return path in MCP_DISPATCHED or any(path.startswith(p) for p in MCP_PREFIXES)


def test_every_mcp_sdk_route_is_dispatched_by_main():
    _, asgi = _mcp_world()
    assert [path for _, path in _mcp_routes(asgi) if not _dispatched(path)] == []
    assert set(MCP_PUBLIC) <= {path for _, path in _mcp_routes(asgi)}


async def test_every_mcp_route_refuses_an_anonymous_caller_and_writes_nothing():
    db, asgi = _mcp_world()
    probes = [(m, p) for m, p in _mcp_routes(asgi) if p not in MCP_PUBLIC]
    served = {path for _, path in _mcp_routes(asgi)}
    probes += [("POST", p) for p in sorted(MCP_DISPATCHED - MCP_PREFIXES - served)]   # dispatched, but no SDK route
    failures = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi, raise_app_exceptions=False),
                                 base_url="https://mcp.example.test") as http:
        for method, path in probes:
            spec = _MCP_INPUT.get(path, {})
            resp = await http.request(method, path, **spec)
            if resp.status_code not in _MCP_REFUSED and not (path == "/mcp/" and resp.status_code == 307):
                failures.append(f"{method} {path}: answered {resp.status_code}")
    assert failures == []
    assert {name: col.docs for name, col in db.cols.items() if col.docs} == {}


# --------------------------------------------------------------------------- static companion

def _factory_name(call: ast.Call) -> str:
    return ast.unparse(call.func)


def _registered_factories(path: Path) -> List[str]:
    tree = ast.parse(path.read_text())
    assigned: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned[target.id] = _factory_name(node.value)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "register_blueprint":
            arg = node.args[0]
            if isinstance(arg, ast.Call):
                found.append(_factory_name(arg))
            elif isinstance(arg, ast.Name) and arg.id in assigned:
                found.append(assigned[arg.id])
            else:
                found.append(f"<unresolved {ast.unparse(arg)} in {path.name}>")
    return found


def test_every_registered_blueprint_is_one_the_probe_builds():
    registered = {name for entry in ("main.py", "indexer_main.py", "sandbox_main.py")
                  for name in _registered_factories(ROOT / entry)}
    assert registered == set(FACTORIES)


def test_main_dispatches_exactly_the_probed_mcp_paths():
    tree = ast.parse((ROOT / "main.py").read_text())
    [fn] = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_is_mcp_path"]
    literals = {n.value for n in ast.walk(fn) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert literals == MCP_DISPATCHED
    prefixes = {ast.literal_eval(n.args[0]) for n in ast.walk(fn)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "startswith"}
    assert prefixes == MCP_PREFIXES
