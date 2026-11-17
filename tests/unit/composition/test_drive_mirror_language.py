"""`build_drive_mirror` gives the indexer's transcript mirror the read side of the language settings only: a
`LanguageResolver` over the given user and account repositories, never the write path."""
from unittest.mock import MagicMock

from src.composition.drive_model_setup import build_drive_mirror
from src.services.language_resolver import LanguageResolver


def _build(enabled: bool):
    users, accounts = MagicMock(), MagicMock()
    wiring = build_drive_mirror(enabled=enabled, repo=MagicMock(), mirror_repo=MagicMock(), drive=MagicMock(),
                                users=users, account_repo=accounts)
    return wiring, users, accounts


def test_a_running_mirror_resolves_the_language_over_the_given_repositories():
    wiring, users, accounts = _build(True)

    assert type(wiring.language) is LanguageResolver
    assert wiring.language._user_repo is users and wiring.language._account_repo is accounts
    assert wiring.mirror is not None and wiring.localization is not None


def test_a_mirror_switched_off_builds_no_language():
    wiring, _, _ = _build(False)

    assert (wiring.mirror, wiring.localization, wiring.language) == (None, None, None)
