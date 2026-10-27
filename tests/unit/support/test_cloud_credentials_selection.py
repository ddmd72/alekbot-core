"""The cloud gate takes WIF credentials of the test service account directly and still refuses any other identity."""
from types import SimpleNamespace

import pytest

from tests.support.cloud_credentials import select_credentials

SA = "test-sa@example.invalid"


def _creds(email):
    return SimpleNamespace(service_account_email=email)


def _recorder(result):
    seen = []

    def impersonate(source):
        seen.append(source)
        return result

    return impersonate, seen


def test_source_already_the_test_sa_is_used_without_impersonation():
    source = _creds(SA)
    impersonate, seen = _recorder(_creds("never@example.invalid"))
    assert select_credentials(source, SA, impersonate) is source
    assert seen == []


def test_other_source_is_impersonated():
    source, impersonated = _creds("user@example.invalid"), _creds(SA)
    impersonate, seen = _recorder(impersonated)
    assert select_credentials(source, SA, impersonate) is impersonated
    assert seen == [source]


def test_source_without_email_is_impersonated():
    source, impersonated = object(), _creds(SA)
    impersonate, seen = _recorder(impersonated)
    assert select_credentials(source, SA, impersonate) is impersonated
    assert seen == [source]


def test_impersonation_yielding_another_identity_is_refused():
    source = _creds("x@example.invalid")
    impersonate, seen = _recorder(_creds("other@example.invalid"))
    with pytest.raises(PermissionError):
        select_credentials(source, SA, impersonate)
    assert seen == [source]


def test_impersonation_yielding_an_object_without_email_is_refused():
    source = _creds("x@example.invalid")
    impersonate, seen = _recorder(object())
    with pytest.raises(PermissionError):
        select_credentials(source, SA, impersonate)
    assert seen == [source]
