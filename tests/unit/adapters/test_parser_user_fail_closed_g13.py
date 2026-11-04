"""G13: with no parser user, a Cloud Run process (K_SERVICE set) refuses to start the sandbox; a local run keeps the
warning. A configured user is unchanged."""
import logging

import pytest

from src.adapters import subprocess_content_extractor as content


@pytest.fixture(params=[("content", content._resolve_sandbox)], ids=["content"])
def resolver(request):
    return request.param[1]


@pytest.mark.parametrize("user", [None, "", "  "])
def test_no_user_on_cloud_run_raises_and_logs_an_error(resolver, monkeypatch, caplog, user):
    monkeypatch.setenv("K_SERVICE", "alek-indexer")
    with caplog.at_level(logging.WARNING):
        with pytest.raises(RuntimeError):
            resolver(user)
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_no_user_without_k_service_is_none_with_a_warning(resolver, monkeypatch, caplog):
    monkeypatch.delenv("K_SERVICE", raising=False)
    with caplog.at_level(logging.WARNING):
        assert resolver(None) is None
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


def test_a_configured_user_is_unchanged_on_cloud_run(resolver, monkeypatch):
    monkeypatch.setenv("K_SERVICE", "alek-indexer")
    with pytest.raises(RuntimeError, match="does not exist"):
        resolver("no-such-parser-user-xyz")


@pytest.mark.parametrize("module", [content], ids=["content"])
def test_a_configured_user_resolves_to_its_ids_on_cloud_run(module, monkeypatch):
    monkeypatch.setenv("K_SERVICE", "alek-indexer")
    entry = type("Entry", (), {"pw_uid": 1234, "pw_gid": 5678})()
    monkeypatch.setattr(module.pwd, "getpwnam", lambda name: entry)
    monkeypatch.setattr(module, "_effective_uid", lambda: 0)
    resolve = getattr(module, "resolve_sandbox", None) or module._resolve_sandbox
    assert resolve("drive-parser") == (1234, 5678)
