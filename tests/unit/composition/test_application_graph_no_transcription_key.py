"""The application graph without an OpenAI key: voice messages stay an honest alert (no audio service) and the log says
transcription is off."""
import logging

import pytest

from tests.support import prod_env


@pytest.mark.slow
def test_no_openai_key_means_no_audio_service_and_an_info_line(monkeypatch, caplog):
    from src.composition.application_graph import build_application_graph
    from src.config.settings import load_settings

    offline = prod_env.install_offline_services(monkeypatch)
    config = load_settings()
    config["OPENAI_API_KEY"] = ""
    caplog.set_level(logging.INFO)
    app = build_application_graph(config, offline.db)
    assert app.slack_adapter.conversation_handler.audio_service is None
    assert any(r.levelno == logging.INFO and "Audio transcription disabled" in r.getMessage()
               for r in caplog.records)
