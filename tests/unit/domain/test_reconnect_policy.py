"""R1's rule (SECRETS_AT_REST_RFC §6.2, F1/F2): Reconnect shows when a past connection has no credential and was not
disconnected on purpose; the one notice is due only while that transition is not notified yet."""
from itertools import product

import pytest

from src.domain.reconnect_policy import Integration, r1_due, reconnect_needed


def test_a_past_connection_without_a_credential_needs_a_reconnect_and_one_notice():
    assert reconnect_needed(has_credential=False, past_connection=True, disconnected_by_user=False) is True
    assert r1_due(has_credential=False, past_connection=True, disconnected_by_user=False, notified=False) is True


def test_a_notified_transition_still_shows_reconnect_but_sends_no_second_notice():
    assert reconnect_needed(has_credential=False, past_connection=True, disconnected_by_user=False) is True
    assert r1_due(has_credential=False, past_connection=True, disconnected_by_user=False, notified=True) is False


@pytest.mark.parametrize("has_credential,past_connection,disconnected_by_user", [
    (True, True, False),     # the credential is there: nothing lost
    (False, False, False),   # never connected (or a To Do config reset by its disconnect)
    (False, True, True),     # a deliberate disconnect is not a loss (F1)
])
def test_no_reconnect_and_no_notice(has_credential, past_connection, disconnected_by_user):
    assert reconnect_needed(has_credential, past_connection, disconnected_by_user) is False
    assert r1_due(has_credential, past_connection, disconnected_by_user, notified=False) is False


def test_due_exactly_when_needed_and_not_notified_over_every_input():
    for has, past, disc, notified in product((False, True), repeat=4):
        expected = (not has) and past and (not disc) and (not notified)
        assert r1_due(has, past, disc, notified) is expected, (has, past, disc, notified)


def test_the_three_integrations_have_stable_names():
    assert [i.value for i in Integration] == ["gmail", "drive", "todo"]
