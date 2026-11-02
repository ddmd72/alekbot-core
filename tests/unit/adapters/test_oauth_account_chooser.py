"""Owner decision 2026-11-02: every connect (first, add, reconnect) of every realm shows the account chooser."""
import pytest

from src.domain.oauth_flow import FlowIntent
from tests.contracts.oauth_provider_contract import REDIRECT, _flow, _query, case  # noqa: F401


@pytest.mark.parametrize("intent", [FlowIntent.CONNECT, FlowIntent.ADD, FlowIntent.RECONNECT])
def test_every_intent_shows_the_account_chooser(case, intent) -> None:  # noqa: F811
    query = _query(case.edge.authorization_url(_flow(intent, "alex@example.com"), case.spec, REDIRECT, state="s"))
    assert "select_account" in query["prompt"].split()
    if case.realm == "google":
        assert "consent" in query["prompt"].split()      # the refresh token needs the consent screen
