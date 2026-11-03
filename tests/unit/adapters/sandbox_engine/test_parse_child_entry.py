"""parse_child.py entry: the repository root is appended LAST to sys.path (nothing can shadow a stdlib module) and
`main` hands over to the drive parser's own child module by name."""
import importlib.util
import os
import runpy
import sys

from src.adapters.sandbox_engine import parse_child


def _load_fresh():
    spec = importlib.util.spec_from_file_location("parse_child_fresh", parse_child.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_repository_root_is_appended_last_when_missing(monkeypatch):
    root = os.path.abspath(os.path.join(os.path.dirname(parse_child.__file__), "..", "..", ".."))
    monkeypatch.setattr(sys, "path", [p for p in sys.path if os.path.abspath(p or ".") != root])
    module = _load_fresh()
    assert module._REPO_ROOT == root and sys.path[-1] == root


def test_main_runs_the_drive_child_module_as_main_and_returns_zero(monkeypatch):
    calls = []
    monkeypatch.setattr(runpy, "run_module", lambda name, run_name=None: calls.append((name, run_name)))
    assert parse_child.main() == 0
    assert calls == [("src.adapters.drive_parser.child", "__main__")]
