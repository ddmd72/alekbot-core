"""
GCP Cloud Tasks Adapter
=======================

Concrete TaskQueue implementation for Google Cloud Tasks.
"""
import json
import datetime
from typing import Dict, Any, Optional

from google.cloud import tasks_v2
from google.protobuf import timestamp_pb2

from ..ports.task_queue import TaskQueue
from ..utils.logger import logger


class GcpTaskQueue(TaskQueue):
    """Adapter for Google Cloud Tasks."""

    def __init__(
        self,
        project_id: str,
        location: str,
        queue_name: str,
        service_url: str,
        service_account_email: Optional[str] = None
    ):
        self.client = tasks_v2.CloudTasksClient()
        self.project_id = project_id
        self.location = location
        self.queue_name = queue_name
        self.service_url = service_url
        self.service_account_email = service_account_email

        self.queue_path = self.client.queue_path(project_id, location, queue_name)

        logger.info(f"📬 GcpTaskQueue initialized (queue: {queue_name}, location: {location})")

    async def enqueue_slack_event(
        self,
        event_data: Dict[str, Any],
        session_id: str,
        delay_seconds: int = 0,
        trace_headers: Optional[Dict[str, str]] = None
    ) -> str:
        try:
            payload = {
                "event": event_data,
                "session_id": session_id,
                "enqueued_at": datetime.datetime.utcnow().isoformat()
            }

            headers = {
                "Content-Type": "application/json"
            }
            if trace_headers:
                headers.update(trace_headers)

            task = {
                "http_request": {
                    "http_method": tasks_v2.HttpMethod.POST,
                    "url": f"{self.service_url}/worker",
                    "headers": headers,
                    "body": json.dumps(payload).encode()
                }
            }

            if self.service_account_email:
                task["http_request"]["oidc_token"] = {
                    "service_account_email": self.service_account_email
                }

            if delay_seconds > 0:
                timestamp = timestamp_pb2.Timestamp()
                timestamp.FromDatetime(
                    datetime.datetime.utcnow() + datetime.timedelta(seconds=delay_seconds)
                )
                task["schedule_time"] = timestamp

            response = self.client.create_task(
                request={
                    "parent": self.queue_path,
                    "task": task
                }
            )

            return response.name

        except Exception as e:
            logger.error(f"❌ Failed to enqueue task: {e}", exc_info=True)
            raise

    async def create_queue_if_not_exists(self) -> None:
        try:
            try:
                self.client.get_queue(name=self.queue_path)
                logger.info(f"✅ Queue {self.queue_name} already exists")
                return
            except Exception:
                pass

            parent = self.client.common_location_path(self.project_id, self.location)
            queue = {
                "name": self.queue_path,
                "rate_limits": {
                    "max_dispatches_per_second": 10,
                    "max_concurrent_dispatches": 5
                },
                "retry_config": {
                    "max_attempts": 3,
                    "max_retry_duration": {"seconds": 600},
                    "min_backoff": {"seconds": 10},
                    "max_backoff": {"seconds": 300},
                    "max_doublings": 5
                }
            }

            self.client.create_queue(
                request={
                    "parent": parent,
                    "queue": queue
                }
            )

            logger.info(f"✅ Queue {self.queue_name} created successfully")

        except Exception as e:
            logger.error(f"❌ Failed to create queue: {e}", exc_info=True)
            raise

    def get_queue_stats(self) -> Dict[str, Any]:
        try:
            queue = self.client.get_queue(name=self.queue_path)
            return {
                "name": queue.name,
                "state": queue.state.name,
                "max_dispatches_per_second": queue.rate_limits.max_dispatches_per_second,
                "max_concurrent_dispatches": queue.rate_limits.max_concurrent_dispatches
            }
        except Exception as e:
            logger.error(f"❌ Failed to get queue stats: {e}")
            return {}

    async def purge_queue(self) -> None:
        try:
            self.client.purge_queue(name=self.queue_path)
            logger.warning(f"🗑️ Queue {self.queue_name} purged")
        except Exception as e:
            logger.error(f"❌ Failed to purge queue: {e}")
            raise

    async def enqueue_agent_task(
        self,
        agent_id: str,
        intent: str,
        query: str,
        context: Dict[str, Any]
    ) -> str:
        """Enqueue async agent task for background execution via Cloud Tasks."""
        try:
            payload = {
                "task_type": "agent_execution",
                "agent_id": agent_id,
                "intent": intent,
                "query": query,
                "context": context,
            }

            task = {
                "http_request": {
                    "http_method": tasks_v2.HttpMethod.POST,
                    "url": f"{self.service_url}/worker",
                    "headers": {"Content-Type": "application/json"},
                    "body": json.dumps(payload).encode()
                }
            }

            if self.service_account_email:
                task["http_request"]["oidc_token"] = {
                    "service_account_email": self.service_account_email
                }

            response = self.client.create_task(
                request={"parent": self.queue_path, "task": task}
            )

            logger.info(f"Enqueued agent task: agent={agent_id}, intent={intent}, task={response.name}")
            return response.name

        except Exception as e:
            logger.error(f"❌ Failed to enqueue agent task: {e}", exc_info=True)
            raise

    async def enqueue_consolidation_task(self, user_id: str) -> str:
        """Enqueue consolidation task via Cloud Tasks — gives it its own HTTP request + full CPU."""
        try:
            payload = {
                "task_type": "consolidation",
                "user_id": user_id,
            }

            task = {
                "http_request": {
                    "http_method": tasks_v2.HttpMethod.POST,
                    "url": f"{self.service_url}/worker",
                    "headers": {"Content-Type": "application/json"},
                    "body": json.dumps(payload).encode()
                }
            }

            if self.service_account_email:
                task["http_request"]["oidc_token"] = {
                    "service_account_email": self.service_account_email
                }

            response = self.client.create_task(
                request={"parent": self.queue_path, "task": task}
            )

            logger.info(f"📦 Enqueued consolidation task for user {user_id[:8]}: {response.name}")
            return response.name

        except Exception as e:
            logger.error(f"❌ Failed to enqueue consolidation task: {e}", exc_info=True)
            raise