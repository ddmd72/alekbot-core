import pytest
import json
from unittest.mock import MagicMock, patch
from src.services.cloud_tasks_service import CloudTasksService

@pytest.mark.requirement("REQ-CORE-06")
@pytest.mark.asyncio
async def test_task_queue_enqueue_logic():
    """
    Integration test for CloudTasksService enqueue logic.
    Covers: REQ-CORE-06 (Task Queueing)
    """
    project_id = "test-project"
    location = "europe-west1"
    queue_name = "test-queue"
    service_url = "https://test-service.a.run.app"
    
    # Mock the CloudTasksClient
    with patch("google.cloud.tasks_v2.CloudTasksClient") as mock_client_class:
        mock_client = mock_client_class.return_value
        mock_client.queue_path.return_value = f"projects/{project_id}/locations/{location}/queues/{queue_name}"
        
        service = CloudTasksService(
            project_id=project_id,
            location=location,
            queue_name=queue_name,
            service_url=service_url
        )
        
        event_data = {"type": "message", "text": "hello"}
        session_id = "session-123"
        
        # Mock create_task response
        mock_response = MagicMock()
        mock_response.name = "projects/test/locations/test/queues/test/tasks/123"
        mock_client.create_task.return_value = mock_response
        
        task_name = await service.enqueue_slack_event(event_data, session_id)
        
        assert task_name == mock_response.name
        
        # Verify create_task call
        args, kwargs = mock_client.create_task.call_args
        request = kwargs['request']
        task = request['task']
        
        assert request['parent'] == service.queue_path
        assert task['http_request']['url'] == f"{service_url}/worker"
        
        # Verify payload
        payload = json.loads(task['http_request']['body'].decode())
        assert payload['event'] == event_data
        assert payload['session_id'] == session_id
        assert 'enqueued_at' in payload
