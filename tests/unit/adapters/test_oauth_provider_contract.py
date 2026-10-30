"""Runs the OAuthProviderPort contract suite (tests/contracts/oauth_provider_contract.py) in the unit collection."""
from tests.contracts.oauth_provider_contract import (  # noqa: F401
    case,
    test_authorization_url_carries_state_pkce_and_scopes,
    test_authorization_url_prompt_per_intent,
    test_edge_identity,
    test_exchange_identity_failure_raises_without_body,
    test_exchange_missing_subject_raises,
    test_exchange_non_200_raises_without_leaking,
    test_exchange_posts_verifier_and_returns_grant,
    test_google_revoke_failure_does_not_raise,
    test_login_hint_on_reconnect_only,
    test_microsoft_exchange_posts_scope,
    test_revoke_honours_scope,
)
