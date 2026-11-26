"""The tasks MCP resource's contract (EXTERNAL_TASK_QUEUE_RFC §5.4, §5.12): the seven tools in their order, each
tool's parameters in their order (none names a user, an account, a worker or a label), and the exact key sets of its
two tokens."""
from src.domain import external_task_mcp_contract as c
from src.domain.drive_connector_contract import INSTRUCTIONS_KEY
from src.domain.external_task_refusal import TaskRefusal


def test_the_seven_tools_and_their_parameters_in_order():
    assert c.TOOL_NAMES == ("list_tasks", "claim_task", "renew_lease", "submit_task_result", "release_task",
                            "fail_task", "get_task_file")
    assert dict(c.TOOL_PARAMETERS) == {
        "list_tasks": (), "claim_task": (), "renew_lease": ("task_id", "lease_id"),
        "submit_task_result": ("task_id", "lease_id", "result"), "release_task": ("task_id", "lease_id"),
        "fail_task": ("task_id", "lease_id", "reason"), "get_task_file": ("task_id", "lease_id", "file")}
    assert list(c.TOOL_PARAMETERS) == list(c.TOOL_NAMES)


def test_no_tool_takes_a_user_an_account_a_worker_or_a_label():
    params = {p for ps in c.TOOL_PARAMETERS.values() for p in ps}
    assert not params & {"user_id", "account_id", "worker_id", "worker", "label", "user", "account"}


def test_only_the_listing_is_read_only():
    assert c.READ_ONLY_TOOLS == {"list_tasks"}


def test_the_tool_token_holds_the_instructions_each_tool_and_each_parameter():
    assert c.REQUIRED_TOOL_KEYS == {
        INSTRUCTIONS_KEY, *c.TOOL_NAMES, "renew_lease.task_id", "renew_lease.lease_id", "submit_task_result.task_id",
        "submit_task_result.lease_id", "submit_task_result.result", "release_task.task_id", "release_task.lease_id",
        "fail_task.task_id", "fail_task.lease_id", "fail_task.reason", "get_task_file.task_id",
        "get_task_file.lease_id", "get_task_file.file"}


def test_the_result_token_holds_every_line_and_one_line_per_worker_refusal():
    assert c.REFUSAL_KEYS == ("E_NOT_FOUND", "E_STALE_LEASE", "E_LEASE_EXPIRED", "E_RENEW_CAP", "E_ALREADY_SUBMITTED",
                              "E_ALREADY_FINAL", "E_TOO_LARGE", "E_INVALID_TEXT", "E_NO_SUCH_FILE", "E_FILE_GONE",
                              "E_LINK_UNAVAILABLE")
    assert c.REQUIRED_RESULT_KEYS == {
        "CLAIMED", "WRITTEN_BY", "TITLE", "TASK_TEXT", "NO_TASK", "LIST_HEAD", "LIST_QUEUED", "LIST_LEASED",
        "LIST_EMPTY", "RENEWED", "SUBMITTED", "RELEASED", "FAILED", "ID_REQUIRED", "RATE_LIMITED", "REFUSED",
        "FILES_HEAD", "FILE_LINE", "FILE_LINK", *c.REFUSAL_KEYS}


def test_the_access_refusals_share_the_neutral_line():
    for code in (TaskRefusal.NOT_ENABLED, TaskRefusal.WORKER_REVOKED, TaskRefusal.ACCOUNT_LOCKED):
        assert code not in c.WORKER_REFUSALS


def test_every_refusal_line_carries_its_code_and_only_the_size_line_the_cap():
    for key in c.REFUSAL_KEYS:
        expected = {"code", "max_bytes"} if key == "E_TOO_LARGE" else {"code"}
        assert c.RESULT_PLACEHOLDERS[key] == expected, key
    assert set(c.RESULT_PLACEHOLDERS) <= c.REQUIRED_RESULT_KEYS
    assert {k for k in c.REQUIRED_RESULT_KEYS if k not in c.RESULT_PLACEHOLDERS} == {
        "WRITTEN_BY", "TASK_TEXT", "NO_TASK", "LIST_EMPTY", "REFUSED"}
