"""setup_logger picks the structured handler on Cloud Run for both a service (K_SERVICE) and a job (CLOUD_RUN_JOB)."""
import logging
import os
import sys
import types
from unittest.mock import patch

import pytest

from src.utils.logger import AlekFormatter, _CloudContextFilter, setup_logger


class _RecordingHandler(logging.Handler):
    def __init__(self, stream=None):
        super().__init__()
        self.stream = stream

    def emit(self, record):
        pass


@pytest.fixture
def clean_root():
    root = logging.getLogger()
    saved = root.handlers[:]
    try:
        yield root
    finally:
        root.handlers.clear()
        root.handlers.extend(saved)


def _run_setup(env):
    fake_module = types.ModuleType("google.cloud.logging.handlers")
    fake_module.StructuredLogHandler = _RecordingHandler
    base = {k: v for k, v in os.environ.items() if k not in ("K_SERVICE", "CLOUD_RUN_JOB", "ANTHROPIC_LOG")}
    base.update(env)
    with (
        patch.dict(os.environ, base, clear=True),
        patch.dict(sys.modules, {"google.cloud.logging.handlers": fake_module}),
        patch("src.utils.logger.logging.FileHandler", return_value=logging.NullHandler()) as file_handler,
    ):
        logging.getLogger().handlers.clear()  # pytest's capture handlers would make setup_logger return early
        setup_logger()
    return file_handler


def _assert_structured(root, file_handler):
    assert len(root.handlers) == 1
    handler = root.handlers[0]
    assert type(handler) is _RecordingHandler
    assert handler.level == logging.INFO
    assert any(isinstance(f, _CloudContextFilter) for f in handler.filters)
    file_handler.assert_not_called()


def test_a_cloud_run_job_gets_the_structured_handler(clean_root):
    file_handler = _run_setup({"CLOUD_RUN_JOB": "research-job"})
    _assert_structured(clean_root, file_handler)


def test_a_cloud_run_service_still_gets_it(clean_root):
    file_handler = _run_setup({"K_SERVICE": "svc-1"})
    _assert_structured(clean_root, file_handler)


def test_with_neither_variable_the_local_handlers_are_used(clean_root):
    file_handler = _run_setup({})
    file_handler.assert_called_once()
    console = [h for h in clean_root.handlers if type(h) is logging.StreamHandler]
    assert len(console) == 1
    assert isinstance(console[0].formatter, AlekFormatter)
    assert len(clean_root.handlers) == 2
