"""`build_languages`: one localization port behind the localization service, and the resolver over the given repositories
with SYSTEM_DEFAULT_LANGUAGE as its default (English when unset or unknown)."""
import dataclasses

import pytest

from src.composition.language_setup import build_languages
from src.domain.language import LanguageCode

USERS, ACCOUNTS = object(), object()


@pytest.mark.parametrize("config, expected", [
    ({"SYSTEM_DEFAULT_LANGUAGE": "uk"}, LanguageCode.UK),
    ({}, LanguageCode.EN),
    ({"SYSTEM_DEFAULT_LANGUAGE": "not-a-language"}, LanguageCode.EN),
])
def test_the_system_default_reaches_the_resolver(config, expected):
    languages = build_languages(config, user_repo=USERS, account_repo=ACCOUNTS)  # type: ignore[arg-type]

    assert languages.system_default is expected
    assert languages.resolver._system_default is expected


def test_the_resolver_reads_the_given_repositories_and_the_service_reads_the_one_port():
    languages = build_languages({}, user_repo=USERS, account_repo=ACCOUNTS)  # type: ignore[arg-type]

    assert languages.resolver._user_repo is USERS and languages.resolver._account_repo is ACCOUNTS
    assert languages.localization._port is languages.localization_port


def test_the_bundle_is_frozen():
    languages = build_languages({}, user_repo=USERS, account_repo=ACCOUNTS)  # type: ignore[arg-type]

    with pytest.raises(dataclasses.FrozenInstanceError):
        languages.resolver = None  # type: ignore[misc]
