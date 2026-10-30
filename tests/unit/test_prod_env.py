"""The production-shaped environment (`tests/support/prod_env.py`) and the recording Cloud Tasks client (P9 design §4)."""
import os

import pytest

from src.adapters.gcp_task_queue import GcpTaskQueue
from tests.support import prod_env
from tests.support.cloud_tasks_fake import FakeCloudTasksClient


def test_the_seal_removes_a_foreign_knob_and_keeps_the_keep_list(monkeypatch):
    monkeypatch.setenv("OPENAI_DEEP_RESEARCH_MODEL", "pinned-model")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/nowhere.json")
    monkeypatch.setenv("PYTEST_ADDOPTS_PROBE", "kept")
    monkeypatch.setenv("TZ", "UTC")
    gone = prod_env.seal(monkeypatch)
    assert "OPENAI_DEEP_RESEARCH_MODEL" in gone and "OPENAI_DEEP_RESEARCH_MODEL" not in os.environ
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ
    assert os.environ["PYTEST_ADDOPTS_PROBE"] == "kept" and os.environ["TZ"] == "UTC"
    assert all(prod_env.keeps(name) for name in os.environ)


def test_every_variable_of_the_deploy_step_has_a_substitution_row():
    assert prod_env.missing_substitutions() == []
    assert not [k for k, v in prod_env.deploy_env().items() if "$" in v]


def test_the_makefile_defaults_are_read_and_the_live_overrides_name_real_ones():
    defaults = prod_env.makefile_defaults()
    assert defaults["_SANDBOX_RENDER"] == "inprocess"
    assert defaults["_DRIVE_TRANSCRIBE_ENABLED"] == "off"
    assert defaults["_DRIVE_UPGRADE_PASS"] == "on"
    assert set(prod_env.LIVE_OVERRIDES) <= set(defaults)
    table = prod_env.substitutions()
    # the live value is stated in the deployment docs, not the root CLAUDE.md (slimmed 2026-10-30)
    assert table["_DRIVE_TRANSCRIBE_ENABLED"].value == "on"
    assert "docs/07_deployment/README.md" in table["_DRIVE_TRANSCRIBE_ENABLED"].source
    assert all(row.source for row in table.values())


def test_the_deploy_environment_has_the_deploys_literals_secrets_and_cloud_run_names():
    env = prod_env.deploy_env()
    assert env["APP_ENV"] == "development" and env["FIRESTORE_DATABASE"] == "us-production"
    assert env["GOOGLE_CLOUD_PROJECT"] == prod_env.substitutions()["PROJECT_ID"].value
    assert env["K_SERVICE"] == "alek-bot-dev" and env["K_REVISION"].startswith("alek-bot-dev-")
    assert env["MCP_RESOURCE_URI"] == f"{env['CLOUD_RUN_SERVICE_URL']}/mcp"
    assert set(prod_env.secret_names()) <= set(env)
    assert "OPENAI_DEEP_RESEARCH_MODEL" not in env


def test_the_shaped_secrets_pass_the_validators_that_run_at_startup(monkeypatch):
    from src.config.auth import AuthConfig
    from src.config.environment import validate_telegram_config

    config, _ = prod_env.prod_settings(monkeypatch)
    assert validate_telegram_config() == {
        "token": prod_env.SECRET_VALUES["TELEGRAM_BOT_TOKEN"], "webhook_secret": prod_env.SECRET_VALUES["TELEGRAM_WEBHOOK_SECRET"]}
    AuthConfig(config).validate()


def test_the_real_load_settings_asks_secret_manager_for_nothing(monkeypatch):
    config, fallbacks = prod_env.prod_settings(monkeypatch)
    assert fallbacks == []
    assert config["SERVICE_ACCOUNT_EMAIL"] == prod_env.SECRET_VALUES["SERVICE_ACCOUNT_EMAIL"]
    assert config["SYSTEM_DEFAULT_LANGUAGE"].value == "uk"
    assert config["ENABLE_HTML_RENDERER"] is True


def test_a_key_the_deploy_does_not_provide_shows_up_as_a_fallback(monkeypatch):
    real_names = prod_env.secret_names()
    assert "TWILIO_AUTH_TOKEN" in real_names
    monkeypatch.setattr(prod_env, "secret_names", lambda: [n for n in real_names if n != "TWILIO_AUTH_TOKEN"])
    _, fallbacks = prod_env.prod_settings(monkeypatch)
    assert fallbacks == ["TWILIO_AUTH_TOKEN"]


# ----------------------------------------------------------------------------- the Cloud Tasks client

@pytest.fixture
def tasks(monkeypatch):
    monkeypatch.setattr(FakeCloudTasksClient, "instances", [])
    monkeypatch.setattr("google.cloud.tasks_v2.CloudTasksClient", FakeCloudTasksClient)
    queue = GcpTaskQueue("p", "us-central1", "q", "https://svc.test.example", "invoker@p.iam.gserviceaccount.com")
    return queue, FakeCloudTasksClient.instances[-1]


async def test_a_worker_task_is_recorded_with_its_decoded_body_and_identity(tasks):
    queue, client = tasks
    await queue.enqueue_worker_task("some_task", {"user_id": "u1"}, dedup_key="k")
    [request] = client.create_task_requests
    http = request["task"]["http_request"]
    assert request["parent"] == "projects/p/locations/us-central1/queues/q"
    assert http["url"] == "https://svc.test.example/worker"
    assert http["oidc_token"] == {"service_account_email": "invoker@p.iam.gserviceaccount.com"}
    assert client.created_bodies() == [{"task_type": "some_task", "user_id": "u1"}]
    assert request["task"]["name"].startswith("projects/p/locations/us-central1/queues/q/tasks/some_task-")


async def test_a_missing_queue_is_asked_for_then_created(tasks):
    queue, client = tasks
    await queue.create_queue_if_not_exists()
    path = "projects/p/locations/us-central1/queues/q"
    assert client.get_queue_calls == [path] and client.created_queues == [path]
    await queue.create_queue_if_not_exists()
    assert client.get_queue_calls == [path, path] and client.created_queues == [path]
