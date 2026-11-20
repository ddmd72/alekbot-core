"""`build_operations`: the agent-task queue only with a GCP project, the ops alert sink only with its webhook knob, one
quota service shared by the billing agent and the budget alert."""
import importlib.util

import pytest

from src.adapters.slack.webhook_adapter import SlackWebhookAdapter
from src.composition import operations_setup
from src.composition.deploy_topology import DeployTopology
from src.composition.operations_setup import build_operations

TOPOLOGY = DeployTopology(service_url="https://svc.example.test", queue_suffix="dev", region="region-1",
                          slack_intake_region="region-2")


class _Recorded:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs


@pytest.fixture
def ops(monkeypatch):
    monkeypatch.setattr(operations_setup, "GcpTaskQueue", _Recorded)
    monkeypatch.delenv("BILLING_SLACK_WEBHOOK_URL", raising=False)

    def build(config=None):
        return build_operations(config or {}, topology=TOPOLOGY, account_repo="ACCOUNTS")  # type: ignore[arg-type]
    return build


async def test_with_a_project_the_queue_names_the_deploy_region_suffix_and_service(ops):
    built = ops({"GOOGLE_CLOUD_PROJECT": "project-1", "SERVICE_ACCOUNT_EMAIL": "worker@example.test"})

    assert built.task_queue.kwargs == {"project_id": "project-1", "location": "region-1", "queue_name": "agent-tasks-dev",
                                       "service_url": "https://svc.example.test",
                                       "service_account_email": "worker@example.test"}


async def test_without_a_project_there_is_no_queue(ops):
    assert ops({}).task_queue is None


async def test_the_webhook_knob_gives_one_alert_sink_the_quota_service_posts_to(ops, monkeypatch):
    monkeypatch.setenv("BILLING_SLACK_WEBHOOK_URL", "https://hooks.example.test/ops")

    built = ops()

    assert isinstance(built.alert_sink, SlackWebhookAdapter)
    assert built.alert_sink._webhook_url == "https://hooks.example.test/ops"
    assert built.quota_service.alert_sink is built.alert_sink
    assert built.quota_service.account_repo == "ACCOUNTS"
    assert built.billing_agent.quota_service is built.quota_service


async def test_without_the_webhook_knob_there_is_no_alert_sink(ops):
    built = ops()

    assert built.alert_sink is None and built.quota_service.alert_sink is None


async def test_the_agents_carry_their_ids(ops):
    built = ops()

    assert built.billing_agent.config.agent_id == "billing_agent"


async def test_operations_build_no_logging_agent(ops):
    assert not hasattr(ops(), "logger_agent")
    assert importlib.util.find_spec("src.adapters.gcp_log_sink") is None
