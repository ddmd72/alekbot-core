"""The main service's operations backbone: the agent-task queue, the ops alert sink, the quota service and the
billing agent the coordinator registers."""
import os
from dataclasses import dataclass
from typing import Optional

from ..adapters.firestore_account_repo import FirestoreAccountRepository
from ..adapters.firestore_quota_service import FirestoreQuotaService
from ..adapters.gcp_task_queue import GcpTaskQueue
from ..adapters.slack.webhook_adapter import SlackWebhookAdapter
from ..agents.infrastructure.billing_agent import BillingAgent
from ..domain.agent import AgentConfig
from ..utils.logger import logger
from .deploy_topology import DeployTopology


@dataclass(frozen=True)
class Operations:
    """`task_queue` None: no GCP project (local run). `alert_sink` None: no ops webhook; its consumers then stay quiet."""

    task_queue: Optional[GcpTaskQueue]
    alert_sink: Optional[SlackWebhookAdapter]
    quota_service: FirestoreQuotaService
    billing_agent: BillingAgent


def build_operations(config: dict, *, topology: DeployTopology,
                     account_repo: FirestoreAccountRepository) -> Operations:
    """The queue on the deploy's region and suffix; the alert sink from the optional BILLING_SLACK_WEBHOOK_URL (read
    with os.getenv); the quota service, which posts the daily budget alert to it; the billing agent over the quota service."""
    task_queue = None
    if config.get("GOOGLE_CLOUD_PROJECT"):
        task_queue = GcpTaskQueue(
            project_id=config["GOOGLE_CLOUD_PROJECT"],
            location=topology.region,
            queue_name=f"agent-tasks-{topology.queue_suffix}",
            service_url=topology.service_url,
            service_account_email=config.get("SERVICE_ACCOUNT_EMAIL"),
        )
    else:
        logger.info("📬 Agent task queue: disabled (no GCP project)")

    # Shared by the coordinator (delegation-loop alerts), the LLM 4xx proxy, the billing summary and the budget alert.
    alert_sink = None
    alert_url = os.getenv("BILLING_SLACK_WEBHOOK_URL")
    if alert_url:
        alert_sink = SlackWebhookAdapter(alert_url)
        logger.info("✅ Ops Slack webhook configured (billing + LLM client-error alerts)")

    logger.info("💳 Initializing Billing Agent...")
    quota_service = FirestoreQuotaService(account_repo, alert_sink=alert_sink)
    billing_agent = BillingAgent(
        config=AgentConfig(agent_id="billing_agent", agent_type="billing", timeout_ms=None,
                           capabilities=["usage_tracking", "quota_management"]),
        quota_service=quota_service,
    )
    return Operations(task_queue=task_queue, alert_sink=alert_sink, quota_service=quota_service,
                      billing_agent=billing_agent)
