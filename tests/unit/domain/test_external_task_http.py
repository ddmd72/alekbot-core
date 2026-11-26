"""The HTTP status of each refusal and the address `401` guesses are counted under (EXTERNAL_TASK_QUEUE_RFC §5.5, §5.6)."""
import pytest

from src.domain.external_task_http import UNKNOWN_ADDRESS, client_address, status_for
from src.domain.external_task_refusal import TaskRefusal as R


@pytest.mark.parametrize("reason,status", [
    (R.KEY_INVALID, 401),
    (R.NOT_ENABLED, 403), (R.ACCOUNT_LOCKED, 403), (R.WORKER_REVOKED, 403),
    (R.NOT_FOUND, 404),
    (R.STALE_LEASE, 409), (R.LEASE_EXPIRED, 409), (R.RENEW_CAP, 409), (R.ALREADY_SUBMITTED, 409),
    (R.ALREADY_FINAL, 409),
    (R.TOO_LARGE, 413),
    (R.INVALID_TEXT, 400),
])
def test_each_worker_refusal_has_its_status_of_the_rfc_table(reason, status):
    assert status_for(reason) == status


@pytest.mark.parametrize("reason", [R.UNKNOWN_WORKER, R.HOLD_NOT_FOUND, R.OPEN_TASKS_CAP, R.LABEL_INVALID,
                                    R.LABEL_TAKEN, R.WORKERS_CAP, R.KEYS_CAP, R.DESTINATION_UNAVAILABLE])
def test_a_refusal_a_worker_never_meets_is_a_plain_400(reason):
    assert status_for(reason) == 400


def test_the_address_is_the_rightmost_entry_the_front_end_appended():
    assert client_address("203.0.113.9") == "203.0.113.9"
    assert client_address("198.51.100.1, 203.0.113.9") == "203.0.113.9"


def test_a_forged_left_entry_is_ignored():
    assert client_address("10.9.9.9, 192.0.2.77, 203.0.113.9") == "203.0.113.9"
    assert client_address("1.2.3.4, 203.0.113.9") == client_address("5.6.7.8, 203.0.113.9")


def test_an_ipv6_address_is_kept_whole():
    assert client_address("2001:db8::7") == "2001:db8::7"


@pytest.mark.parametrize("header", [None, "", " ", "203.0.113.9,", "a/b", "x" * 65, "ünï"])
def test_a_missing_or_odd_entry_counts_under_one_shared_name(header):
    assert client_address(header) == UNKNOWN_ADDRESS


@pytest.mark.parametrize("reason,status", [
    (R.NO_SUCH_FILE, 404),       # a file number the task does not carry
    (R.FILE_GONE, 410),          # the copy is no longer stored: asking again will not help
    (R.LINK_UNAVAILABLE, 503),   # signing failed: asking again will
])
def test_each_file_link_refusal_has_its_status(reason, status):
    assert status_for(reason) == status
