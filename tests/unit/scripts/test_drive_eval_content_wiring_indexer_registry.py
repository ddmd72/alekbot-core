"""The eval's reader and triage run on the indexer's model registry, not the main service's (review E minor,
USER_DRIVE_INDEXING_RFC §24.2, §24.11): Claude and OpenAI only (no Gemini fallback: the eval passes no
GEMINI_API_KEY), a process-local breaker, and none of the main
service's wrappers (alerting proxy, prompt caching, the prompt-content store that would copy the fixture scans to
BigQuery). The reader's provider is stamped into the G1/G2 bars and a run on another provider gets no verdict."""
from types import SimpleNamespace

import pytest

from scripts.validation.drive_eval import content_wiring
from scripts.validation.drive_eval.metrics import bar_mismatch, make_bars, recall
from src.composition import drive_model_setup
from src.composition.drive_model_setup import build_drive_context_builder
from src.domain.drive_extraction import READER
from src.services.agent_context_builder import AgentContextBuilder


def test_the_indexers_context_builder_has_only_the_two_file_providers():
    builder = build_drive_context_builder({"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o"})
    assert isinstance(builder, AgentContextBuilder)
    assert set(builder.registry.list_available()) == {"claude", "openai"}


def _stub_gcp(monkeypatch):
    import google.cloud.firestore as firestore_mod
    import src.adapters.firestore_account_repo as account_mod
    import src.composition.service_container as container_mod
    import src.config.settings as settings_mod

    container = SimpleNamespace(context_builder="the main service's builder", assembly_service=object())
    monkeypatch.setattr(firestore_mod, "AsyncClient", lambda **kwargs: object())
    monkeypatch.setattr(settings_mod, "load_settings",
                        lambda: {"ENVIRONMENT_CONFIG": SimpleNamespace(account_collection_name="accounts")})
    monkeypatch.setattr(account_mod, "FirestoreAccountRepository", lambda **kwargs: object())
    monkeypatch.setattr(container_mod, "ServiceContainer", lambda **kwargs: container)


def test_build_real_content_uses_the_indexers_context_builder(monkeypatch):
    _stub_gcp(monkeypatch)
    monkeypatch.setenv("DRIVE_EVAL_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "o-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-key")
    seen = []
    sentinel = object()

    def fake_builder(keys):
        seen.append(dict(keys))
        return sentinel

    monkeypatch.setattr(drive_model_setup, "build_drive_context_builder", fake_builder)
    extractors, triage = content_wiring.build_real_content()
    assert extractors[READER]._context_builder is sentinel and triage._context_builder is sentinel
    assert seen == [{"ANTHROPIC_API_KEY": "a-key", "OPENAI_API_KEY": "o-key"}]


@pytest.mark.parametrize("env, provider", [("", "openai"), ("claude", "claude"), ("openai", "openai")])
def test_the_reader_provider_of_a_run(monkeypatch, env, provider):
    monkeypatch.setenv("DRIVE_EVAL_PROVIDER", env)
    assert content_wiring.reader_provider() == provider


def test_the_reader_provider_is_stamped_and_checked():
    bars = make_bars(recall([]), [], 8, "fake/8", "now", "t", "g", "f", reader_provider="claude")
    assert bars["reader_provider"] == "claude"
    assert bar_mismatch(bars, 8, "t", "g", [], "f", reader_provider="claude") == []
    reasons = bar_mismatch(bars, 8, "t", "g", [], "f", reader_provider="openai")
    assert "the bars were recorded with the reader on claude, this run reads with openai" in reasons
    unstamped = {k: v for k, v in bars.items() if k != "reader_provider"}
    assert ("the bars were recorded with the reader on None, this run reads with claude"
            in bar_mismatch(unstamped, 8, "t", "g", [], "f", reader_provider="claude"))


async def test_run_eval_records_the_reader_provider_and_refuses_another(tmp_path):
    import json

    from scripts.validation.drive_eval.runner import EvalError, run_eval

    from ..services.drive_index_fakes import FakeEmbeddings
    from .drive_eval_samples import GOLD, MANIFEST, fake_content, rome

    tree, gold = tmp_path / "tree.json", tmp_path / "gold.json"
    tree.write_text(json.dumps(MANIFEST, ensure_ascii=False), encoding="utf-8")
    gold.write_text(json.dumps(GOLD, ensure_ascii=False), encoding="utf-8")
    common = dict(embeddings=FakeEmbeddings(), embedding_space="fake/8", place_of=rome, throttle_pause_s=0)
    await run_eval(tree, gold, tmp_path / "bars.json", tmp_path / "r.json", record=True, reader_provider="claude",
                   **common, **fake_content())
    assert json.loads((tmp_path / "bars.json").read_text(encoding="utf-8"))["reader_provider"] == "claude"
    with pytest.raises(EvalError, match="reader on claude, this run reads with openai"):
        await run_eval(tree, gold, tmp_path / "bars.json", tmp_path / "r.json", record=False,
                       reader_provider="openai", **common, **fake_content())


@pytest.mark.slow
def test_the_cli_passes_the_runs_reader_provider(monkeypatch):
    from scripts.validation.drive_eval import __main__ as cli

    seen = {}

    async def fake_run_eval(*args, **kwargs):
        seen.update(kwargs)
        return {"recall": {}, "cases": [], "k": 8, "embedding_space": "x", "verdict": "pass"}

    monkeypatch.setenv("GEMINI_API_KEY", "offline")
    monkeypatch.setenv("DRIVE_EVAL_PROVIDER", "openai")
    monkeypatch.setattr(cli, "run_eval", fake_run_eval)
    monkeypatch.setattr(content_wiring, "build_real_content", lambda: ({}, None))
    assert cli.main(["--report", "/dev/null"]) == 0
    assert seen["reader_provider"] == "openai"
