"""IAM least-privilege Step 0 (C3): one choke point strips the OIDC identity; unset keeps today's behaviour."""
from unittest.mock import patch

import pytest

from src.adapters.gcp_task_queue import GcpTaskQueue


def _queue(email):
    with patch("google.cloud.tasks_v2.CloudTasksClient"):
        return GcpTaskQueue("p", "l", "q", "https://svc.example.invalid", service_account_email=email)


@pytest.mark.parametrize("raw,expected", [
    ("a@b\n", "a@b"), ("  a@b  ", "a@b"), ("a@b", "a@b"), (None, None), ("", None), (" \n", None),
])
def test_identity_is_stripped(raw, expected):
    assert _queue(raw).service_account_email == expected
