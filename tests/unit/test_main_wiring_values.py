"""`main()` hands the web layer the values production gives it (P9 T5b, round 2: the wiring mutants no earlier test killed).

The boot test's recorder reports an Optional parameter left `None`; a required argument passed `None` only bites at request
time. This file boots `main()` once per test in the T1 harness (the deploy's environment, fakes at the SDK boundaries) and
adds recorders around the things `main()` builds itself, then asserts:

- no argument of any `src.web` blueprint factory is `None`, and every argument `main.py` writes as an attribute chain of the
  graph (`app.session_service`, `app.container.repository`, ...) is that very object;
- the Firestore client got the deploy's project and named database;
- the one voice ticket store has the environment's collection and is the one object all three voice consumers hold;
- the voice call setup, the Cloudflare adapter and the MCP search service got the graph's objects, the deploy's keys and limits;
- with no invoker identity configured, the two gates `main()` builds are closed, because the deploy runs on Cloud Run.
"""
import ast
import inspect
from typing import Any, Dict, List, NamedTuple, Optional

import pytest

import main
from src.domain.settings import SearchConfig
from tests.support import prod_env
from tests.unit.test_main_boots_offline import BLUEPRINT_FACTORY, ROOT, Boot, _boot

pytestmark = pytest.mark.slow

# A blueprint argument that is legitimately None in the deployed wiring needs a line here, with the reason. None today.
NONE_IN_PRODUCTION: Dict[str, str] = {}


class Captured(NamedTuple):
    boot: Boot
    env: Dict[str, str]
    app: Any
    db: Any
    firestore_clients: List[dict]
    stores: List[tuple]          # (args, kwargs, instance) of every FirestoreEphemeralStore main() built
    setups: List[tuple]          # VoiceCallSetupService
    sfu: List[tuple]             # CloudflareSfuAdapter
    searches: List[tuple]        # SearchEnrichmentService built by main()
    mcp_builds: List[tuple]      # (kwargs, MCPComponents)
    gates: List[tuple]           # (label, on_cloud_run, gate)


def _recording(cls: type, sink: list) -> type:
    class Recording(cls):  # type: ignore[valid-type, misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            sink.append((args, kwargs, self))
    Recording.__name__ = cls.__name__
    return Recording


async def _instrumented_boot(monkeypatch, caplog, *, without_invoker_identity: bool = False) -> Captured:
    from src.adapters.cloudflare_sfu_adapter import CloudflareSfuAdapter
    from src.services.search_enrichment_service import SearchEnrichmentService
    from src.services.voice_call_setup_service import VoiceCallSetupService

    seen: Dict[str, Any] = {"firestore_clients": [], "stores": [], "setups": [], "sfu": [], "searches": [],
                            "mcp_builds": [], "gates": []}
    offline_real = prod_env.install_offline_services

    def install(monkeypatch_) -> Any:
        services = offline_real(monkeypatch_)
        seen["db"], seen["env"] = services.db, services.env
        fake_client = __import__("google.cloud.firestore", fromlist=["AsyncClient"]).AsyncClient

        def client(*args: Any, **kwargs: Any) -> Any:
            seen["firestore_clients"].append({"args": args, **kwargs})
            return fake_client(*args, **kwargs)

        monkeypatch_.setattr("google.cloud.firestore.AsyncClient", client)
        return services

    graph_real = main.build_application_graph
    gate_real = main.build_oidc_gate
    mcp_real = main.build_mcp_components
    settings_real = main.load_settings

    def graph(config: dict, db: Any) -> Any:
        seen["app"] = graph_real(config, db)
        return seen["app"]

    def gate(accepted: Any, *args: Any, **kwargs: Any) -> Any:
        built = gate_real(accepted, *args, **kwargs)
        seen["gates"].append((kwargs.get("label"), kwargs.get("on_cloud_run"), built))
        return built

    def mcp(**kwargs: Any) -> Any:
        built = mcp_real(**kwargs)
        seen["mcp_builds"].append((kwargs, built))
        return built

    def settings() -> dict:
        config = settings_real()
        if without_invoker_identity:
            for key in ("SERVICE_ACCOUNT_EMAIL", "VOICE_RELAY_SA_EMAIL"):
                config[key] = None
        return config

    monkeypatch.setattr(prod_env, "install_offline_services", install)
    monkeypatch.setattr(main, "build_application_graph", graph)
    monkeypatch.setattr(main, "build_oidc_gate", gate)
    monkeypatch.setattr(main, "build_mcp_components", mcp)
    monkeypatch.setattr(main, "load_settings", settings)
    monkeypatch.setattr(main, "FirestoreEphemeralStore", _recording(main.FirestoreEphemeralStore, seen["stores"]))
    monkeypatch.setattr("src.services.voice_call_setup_service.VoiceCallSetupService",
                        _recording(VoiceCallSetupService, seen["setups"]))
    monkeypatch.setattr("src.adapters.cloudflare_sfu_adapter.CloudflareSfuAdapter", _recording(CloudflareSfuAdapter, seen["sfu"]))
    monkeypatch.setattr("src.services.search_enrichment_service.SearchEnrichmentService",
                        _recording(SearchEnrichmentService, seen["searches"]))

    boot = await _boot(monkeypatch, caplog)
    if boot.probe_error is not None:
        raise boot.probe_error
    return Captured(boot=boot, **seen)


@pytest.fixture
async def booted(monkeypatch, caplog) -> Captured:
    return await _instrumented_boot(monkeypatch, caplog)


@pytest.fixture
async def booted_without_identity(monkeypatch, caplog) -> Captured:
    return await _instrumented_boot(monkeypatch, caplog, without_invoker_identity=True)


def _calls(captured: Captured, name: str) -> List[tuple]:
    return [call for call in captured.boot.calls if call[0] == name]


def _only_call(captured: Captured, name: str) -> dict:
    [(_, original, args, kwargs)] = _calls(captured, name)
    return dict(inspect.signature(original).bind(*args, **kwargs).arguments)


def _attribute_chain(node: ast.AST) -> Optional[List[str]]:
    names = []
    while isinstance(node, ast.Attribute):
        names.append(node.attr)
        node = node.value
    return list(reversed(names)) if isinstance(node, ast.Name) and node.id == "app" else None


def _graph_expressions() -> Dict[str, Dict[str, List[str]]]:
    """Per blueprint factory `main.py` calls: parameter (keyword, or `#<index>`) -> the `app.<chain>` written there."""
    found: Dict[str, Dict[str, List[str]]] = {}
    for node in ast.walk(ast.parse((ROOT / "main.py").read_text())):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        if not BLUEPRINT_FACTORY.match(name):
            continue
        slots = {f"#{i}": arg for i, arg in enumerate(node.args)}
        slots.update({kw.arg: kw.value for kw in node.keywords if kw.arg})
        found[name] = {slot: chain for slot, value in slots.items() if (chain := _attribute_chain(value))}
    return found


def _resolve(app: Any, chain: List[str]) -> Any:
    value = app
    for attribute in chain:
        value = getattr(value, attribute)
    return value


# ----------------------------------------------------------------------------- every blueprint argument

async def test_no_web_blueprint_factory_is_called_with_a_none_argument(booted):
    problems = []
    for name, original, args, kwargs in booted.boot.calls:
        if not original.__module__.startswith("src.web"):
            continue
        bound = inspect.signature(original).bind(*args, **kwargs)
        bound.apply_defaults()
        for parameter, value in bound.arguments.items():
            if value is None and f"{name}.{parameter}" not in NONE_IN_PRODUCTION:
                problems.append(f"{name}.{parameter} is None in the deployed wiring")
    assert not problems, "\n".join(problems)


async def test_every_graph_object_main_hands_to_a_blueprint_is_that_object(booted):
    problems = []
    for name, slots in _graph_expressions().items():
        [(_, original, args, kwargs)] = _calls(booted, name)
        for slot, chain in slots.items():
            got = args[int(slot[1:])] if slot.startswith("#") else kwargs[slot]
            expected = _resolve(booted.app, chain)
            same = got == expected if inspect.ismethod(expected) else got is expected   # a bound method is a new object per access
            if not same:
                problems.append(f"{name}.{slot}: app.{'.'.join(chain)} is {expected!r}, the blueprint got {got!r}")
    assert not problems, "\n".join(problems)


# ----------------------------------------------------------------------------- the Firestore client and the voice store

async def test_the_firestore_client_gets_the_deploys_project_and_named_database(booted):
    assert booted.firestore_clients == [{"args": (), "project": booted.env["GOOGLE_CLOUD_PROJECT"],
                                         "database": booted.env["FIRESTORE_DATABASE"]}]


async def test_one_voice_ticket_store_with_the_environments_collection_serves_all_three_voice_consumers(booted):
    [(args, kwargs, store)] = booted.stores
    env_config = booted.app.agent_factory.env_config
    assert args == (booted.db,)
    assert kwargs == {"collection": env_config.voice_tickets_collection}
    assert env_config.voice_tickets_collection

    control = _only_call(booted, "create_voice_control_plane_blueprint")
    webhook = _only_call(booted, "create_voice_webhook_blueprint")
    web_call = _only_call(booted, "create_voice_web_call_blueprint")
    [(setup_args, _, setup)] = booted.setups
    assert control["ephemeral_store"] is store
    assert webhook["ephemeral_store"] is store
    assert web_call["ephemeral_store"] is store
    assert setup_args[0] is store


# ----------------------------------------------------------------------------- the voice web call and the webhook strings

async def test_the_voice_web_call_gets_its_setup_its_sfu_adapter_and_the_relay_origin(booted):
    env, app = booted.env, booted.app
    web_call = _only_call(booted, "create_voice_web_call_blueprint")
    [(setup_args, _, setup)] = booted.setups
    [(sfu_args, _, sfu)] = booted.sfu

    assert web_call["call_setup"] is setup
    assert web_call["media_room"] is sfu
    assert setup_args == (booted.stores[0][2], app.alert_sink, app.agent_factory.get_lelik)
    assert all(setup_args)
    assert sfu_args == (env["CLOUDFLARE_SFU_APP_ID"], env["CLOUDFLARE_SFU_APP_SECRET"])
    assert web_call["relay_base_url"] == env["VOICE_RELAY_STREAM_URL"]


async def test_the_voice_webhook_gets_the_service_url_and_the_relay_stream_url(booted):
    webhook = _only_call(booted, "create_voice_webhook_blueprint")

    assert webhook["answer_url"] == f"{booted.env['CLOUD_RUN_SERVICE_URL']}/voice/answer"
    assert webhook["relay_stream_url"] == booted.env["VOICE_RELAY_STREAM_URL"]


# ----------------------------------------------------------------------------- the MCP connector

async def test_the_mcp_search_service_gets_the_graphs_stores_and_the_default_limits(booted):
    app = booted.app
    defaults = SearchConfig()
    [(_, kwargs, search)] = booted.searches

    assert kwargs["repository"] is app.container.repository
    assert kwargs["embedding_service"] is app.container.embedding_service
    assert kwargs["keyword_limit"] == defaults.DEFAULT_KEYWORD_LIMIT is not None
    assert kwargs["phrase_one_limit"] == defaults.DEFAULT_PHRASE_ONE_LIMIT is not None
    assert kwargs["phrase_two_limit"] == defaults.DEFAULT_PHRASE_TWO_LIMIT is not None
    assert kwargs["total_limit"] == 30

    [(build_kwargs, components)] = booted.mcp_builds
    assert build_kwargs["search_enrichment_service"] is search
    assert build_kwargs["db_client"] is booted.db
    assert build_kwargs["auth_config"] is app.auth_config
    consent = _only_call(booted, "build_mcp_consent_blueprint")
    assert consent["mcp_service"] is components.authorization_service


# ----------------------------------------------------------------------------- the gates on Cloud Run

async def test_the_two_oidc_gates_main_builds_refuse_everyone_when_no_invoker_identity_is_configured(booted_without_identity):
    gates = {label: (on_cloud_run, gate) for label, on_cloud_run, gate in booted_without_identity.gates}

    assert sorted(gates) == ["Voice control plane", "Worker"]
    for label, (on_cloud_run, gate) in gates.items():
        assert on_cloud_run is True, label
        assert await gate(None) is False, label
        assert await gate(f"Bearer {prod_env.VALID_TOKEN}") is False, label
