"""T6: caller-set loading (sandbox RFC §4.4; plan G18, G24)."""
import logging

import pytest

from src.composition.sandbox_callers import SandboxCallersError, assert_disjoint, load_caller_sets


def test_overlapping_sets_refuse_start():
    with pytest.raises(SandboxCallersError):
        assert_disjoint({"A": frozenset({"x@example.com"}), "B": frozenset({"x@example.com", "y@example.com"})})


def test_disjoint_sets_pass():
    assert_disjoint({"A": frozenset({"x@example.com"}), "B": frozenset({"y@example.com"})})


def test_one_group_may_hold_several_op_prefixes():
    # A group is a caller (indexer: drive.* + media.*); only the group's email set is checked, never the prefixes.
    env = {"SANDBOX_CALLERS_INDEXER": "idx@example.com", "SANDBOX_CALLERS_MAIN": "main@example.com"}
    sets = load_caller_sets(env)
    assert sets == {"INDEXER": frozenset({"idx@example.com"}), "MAIN": frozenset({"main@example.com"})}


def test_empty_set_on_cloud_run_refuses_start():
    with pytest.raises(SandboxCallersError):
        load_caller_sets({"K_SERVICE": "svc", "SANDBOX_CALLERS_RENDER": ""})
    with pytest.raises(SandboxCallersError):
        load_caller_sets({"K_SERVICE": "svc"})


def test_empty_locally_is_allowed():
    assert load_caller_sets({}) == {}
    assert load_caller_sets({"SANDBOX_CALLERS_RENDER": ""}) == {}


def test_emails_are_normalised():
    sets = load_caller_sets({"SANDBOX_CALLERS_RENDER": "  Main@Example.COM : b@example.com  "})
    assert sets == {"RENDER": frozenset({"main@example.com", "b@example.com"})}


def test_colon_separated_list_and_comma_rejected_loudly():
    assert load_caller_sets({"SANDBOX_CALLERS_RENDER": "a@example.com:b@example.com"})["RENDER"] == frozenset(
        {"a@example.com", "b@example.com"}
    )
    with pytest.raises(SandboxCallersError):
        load_caller_sets({"SANDBOX_CALLERS_RENDER": "a@example.com,b@example.com"})


def test_no_email_value_in_any_log_line(caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(SandboxCallersError) as exc:
        load_caller_sets({"SANDBOX_CALLERS_RENDER": "secret1@example.com,secret2@example.com"})
    with pytest.raises(SandboxCallersError) as exc2:
        assert_disjoint({"A": frozenset({"secret3@example.com"}), "B": frozenset({"secret3@example.com"})})
    assert "Sandbox callers" in caplog.text
    assert "secret" not in caplog.text
    text = caplog.text + str(exc.value) + str(exc2.value)
    assert "secret" not in text
    assert "RENDER" in text


@pytest.mark.parametrize("bad", ["no-at.example.com", "a@@example.com", "a@b@example.com", "a b@example.com",
                                 "a@example.com;b@example.com", "a@example.com\tb"])
def test_malformed_entries_rejected_naming_only_the_group(bad):
    with pytest.raises(SandboxCallersError) as exc:
        load_caller_sets({"SANDBOX_CALLERS_RENDER": bad})
    assert "RENDER" in str(exc.value) and "example.com" not in str(exc.value)
