"""The cloud gate takes WIF credentials of the test service account directly and still refuses any other identity."""
from types import SimpleNamespace

import pytest

from tests.support.cloud_credentials import select_credentials

SA = "test-sa@example.invalid"


def _creds(email):
    return SimpleNamespace(service_account_email=email)


def test_source_already_the_test_sa_is_used_without_impersonation():
    source = _creds(SA)

    def impersonate(_):
        raise AssertionError("must not impersonate")

    assert select_credentials(source, SA, impersonate) is source


def test_other_source_is_impersonated():
    impersonated = _creds(SA)
    assert select_credentials(_creds("user@example.invalid"), SA, lambda s: impersonated) is impersonated


def test_source_without_email_is_impersonated():
    impersonated = _creds(SA)
    assert select_credentials(object(), SA, lambda s: impersonated) is impersonated


def test_impersonation_yielding_another_identity_is_refused():
    with pytest.raises(PermissionError):
        select_credentials(_creds("x@example.invalid"), SA, lambda s: _creds("other@example.invalid"))
