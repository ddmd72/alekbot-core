"""The indexer's model registry registers Gemini when GEMINI_API_KEY is set (USER_DRIVE_INDEXING_RFC §24.2 D7 rev
3.17): it serves only the drive agents' fallback. Without the key the agents run on their primary alone, and the
indexer says so at start."""
from unittest.mock import MagicMock

from src.adapters.gemini_adapter import GeminiAdapter
from src.composition import drive_model_setup
from src.composition.drive_model_setup import build_drive_model_registry


def test_gemini_registered_with_its_key_stripped():
    registry = build_drive_model_registry({"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o", "GEMINI_API_KEY": " g\n"})
    assert set(registry.list_available()) == {"claude", "openai", "gemini"}
    assert isinstance(registry.get("gemini"), GeminiAdapter)


def test_no_gemini_key_warns_that_the_fallback_is_off(monkeypatch):
    log = MagicMock()
    monkeypatch.setattr(drive_model_setup, "logger", log)
    registry = build_drive_model_registry({"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o"})
    assert "gemini" not in registry.list_available()
    messages = [c.args[0] for c in log.warning.call_args_list]
    assert any("GEMINI_API_KEY" in m and "fallback" in m for m in messages)


def test_all_keys_set_warns_nothing(monkeypatch):
    log = MagicMock()
    monkeypatch.setattr(drive_model_setup, "logger", log)
    build_drive_model_registry({"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o", "GEMINI_API_KEY": "g"})
    log.warning.assert_not_called()


def test_the_indexer_passes_the_gemini_key_to_the_model_wiring():
    """indexer_main mounts GEMINI_API_KEY for embeddings; the model wiring must receive it too."""
    from pathlib import Path
    source = Path(__file__).resolve().parents[3].joinpath("indexer_main.py").read_text(encoding="utf-8")
    keys_line = next(line for line in source.splitlines() if "keys={key: os.environ.get(key)" in line)
    assert "GEMINI_API_KEY" in keys_line
