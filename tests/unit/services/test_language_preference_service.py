"""
Unit tests for LanguagePreferenceService and LanguageResolver.

Covers:
- set_preference(): override token selection, user persistence, cache invalidation,
  notification dispatch, error isolation
- LanguageResolver.get_preference(): defaults and stored values
- LanguageResolver.resolve_ui_language(): 3-level resolution chain (user → account → system)
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call
import pytest

from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.domain.billing import BillingAccount
from src.domain.language import LanguageCode
from src.domain.prompt_v3.profile_slot import ProfileToken
from src.domain.prompt_v3.slot import OwnerType
from src.domain.user import UserBotConfig, UserProfile
from src.ports.account_repository import AccountRepository
from src.ports.prompt_builder_port import PromptBuilderPort
from src.ports.prompt_v3.agent_profile_repository import AgentProfileRepository
from src.ports.user_repository import UserRepository
from src.services.language_preference_service import (
    LanguagePreferenceService,
    _ALL_LANG_TOKEN_IDS,
    _LANG_ALERTS,
)
from src.services.language_resolver import LanguageResolver
from tests.support.firestore_eval import EvalDb

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USER_ID = "user-abc"
_ACCOUNT_ID = "account-xyz"


def _make_user(
    preferred_language=None,
    agent_mirror=True,
    account_id=_ACCOUNT_ID,
) -> UserProfile:
    user = UserProfile(user_id=_USER_ID, account_id=account_id)
    user.config.preferred_language = preferred_language
    user.config.agent_mirror = agent_mirror
    return user


def _make_account(default_language=None) -> BillingAccount:
    return BillingAccount(account_id=_ACCOUNT_ID, default_language=default_language)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def user_repo() -> AsyncMock:
    repo = AsyncMock(spec=UserRepository)
    repo.get_user.return_value = _make_user()
    return repo


@pytest.fixture
def account_repo() -> AsyncMock:
    repo = AsyncMock(spec=AccountRepository)
    repo.get_account.return_value = _make_account()
    return repo


@pytest.fixture
def profile_repo() -> AsyncMock:
    return AsyncMock(spec=AgentProfileRepository)


@pytest.fixture
def prompt_builder() -> MagicMock:
    builder = MagicMock(spec=PromptBuilderPort)
    builder.invalidate_cache = MagicMock()
    return builder


@pytest.fixture
def notification_service() -> AsyncMock:
    svc = AsyncMock()
    svc.notify = AsyncMock()
    return svc


@pytest.fixture
def resolver(user_repo, account_repo) -> LanguageResolver:
    return LanguageResolver(
        user_repo=user_repo,
        account_repo=account_repo,
        system_default_language=LanguageCode.EN,
    )


@pytest.fixture
def service(user_repo, profile_repo, prompt_builder, resolver) -> LanguagePreferenceService:
    return LanguagePreferenceService(
        user_repo=user_repo,
        profile_repo=profile_repo,
        prompt_builder=prompt_builder,
        resolver=resolver,
    )


@pytest.fixture
def service_with_notify(
    user_repo, profile_repo, prompt_builder, resolver, notification_service
) -> LanguagePreferenceService:
    svc = LanguagePreferenceService(
        user_repo=user_repo,
        profile_repo=profile_repo,
        prompt_builder=prompt_builder,
        resolver=resolver,
        notification_service=notification_service,
    )
    return svc


# ---------------------------------------------------------------------------
# Tests: set_preference() — override token selection
# ---------------------------------------------------------------------------


class TestSetPreferenceOverrideTokens:
    """set_preference() writes the correct LANG_* override token."""

    async def test_mirror_mode_clears_all_lang_tokens(self, service, profile_repo):
        """agent_mirror=True clears all LANG_* ids and writes empty tokens dict."""
        await service.set_preference(_USER_ID, preferred_language=None, agent_mirror=True)

        profile_repo.set_override_tokens.assert_awaited_once_with(
            OwnerType.USER, _USER_ID, {},
            clear_ids=_ALL_LANG_TOKEN_IDS,
        )

    async def test_fixed_en_writes_lang_fixed_en_token(self, service, profile_repo):
        await service.set_preference(_USER_ID, preferred_language=LanguageCode.EN, agent_mirror=False)

        profile_repo.set_override_tokens.assert_awaited_once_with(
            OwnerType.USER, _USER_ID,
            {"LANG_FIXED_EN": ProfileToken(token_id="LANG_FIXED_EN", order=70, non_overridable=False)},
            clear_ids=_ALL_LANG_TOKEN_IDS - {"LANG_FIXED_EN"},
        )

    async def test_fixed_uk_writes_lang_fixed_uk_token(self, service, profile_repo):
        await service.set_preference(_USER_ID, preferred_language=LanguageCode.UK, agent_mirror=False)

        profile_repo.set_override_tokens.assert_awaited_once_with(
            OwnerType.USER, _USER_ID,
            {"LANG_FIXED_UK": ProfileToken(token_id="LANG_FIXED_UK", order=70, non_overridable=False)},
            clear_ids=_ALL_LANG_TOKEN_IDS - {"LANG_FIXED_UK"},
        )

    async def test_fixed_fr_writes_lang_fixed_fr_token(self, service, profile_repo):
        await service.set_preference(_USER_ID, preferred_language=LanguageCode.FR, agent_mirror=False)

        profile_repo.set_override_tokens.assert_awaited_once_with(
            OwnerType.USER, _USER_ID,
            {"LANG_FIXED_FR": ProfileToken(token_id="LANG_FIXED_FR", order=70, non_overridable=False)},
            clear_ids=_ALL_LANG_TOKEN_IDS - {"LANG_FIXED_FR"},
        )

    async def test_fixed_es_writes_lang_fixed_es_token(self, service, profile_repo):
        await service.set_preference(_USER_ID, preferred_language=LanguageCode.ES, agent_mirror=False)

        profile_repo.set_override_tokens.assert_awaited_once_with(
            OwnerType.USER, _USER_ID,
            {"LANG_FIXED_ES": ProfileToken(token_id="LANG_FIXED_ES", order=70, non_overridable=False)},
            clear_ids=_ALL_LANG_TOKEN_IDS - {"LANG_FIXED_ES"},
        )

    async def test_fixed_no_language_falls_back_to_system_default(self, service, profile_repo):
        """agent_mirror=False with preferred_language=None → system default (EN)."""
        await service.set_preference(_USER_ID, preferred_language=None, agent_mirror=False)

        profile_repo.set_override_tokens.assert_awaited_once_with(
            OwnerType.USER, _USER_ID,
            {"LANG_FIXED_EN": ProfileToken(token_id="LANG_FIXED_EN", order=70, non_overridable=False)},
            clear_ids=_ALL_LANG_TOKEN_IDS - {"LANG_FIXED_EN"},
        )

    async def test_clear_ids_contains_all_other_lang_tokens(self, service, profile_repo):
        await service.set_preference(_USER_ID, preferred_language=LanguageCode.EN, agent_mirror=False)

        clear_ids = profile_repo.set_override_tokens.await_args.kwargs["clear_ids"]
        assert {"LANG_MIRROR", "LANG_FIXED_UK", "LANG_FIXED_FR", "LANG_FIXED_ES"} <= clear_ids


# ---------------------------------------------------------------------------
# Tests: set_preference() — user persistence
# ---------------------------------------------------------------------------


_USERS_ENV = SimpleNamespace(domain_users_collection="users", is_production=False)


async def _stored_service(profile_repo, prompt_builder, user: UserProfile):
    """The service over the real FirestoreUserRepository on the evaluating fake, `user` stored; returns the service
    and a reader of the stored profile document."""
    db = EvalDb()
    accounts = FirestoreAccountRepository(db, "accounts")
    users = FirestoreUserRepository(db, _USERS_ENV, accounts)
    await users.create_user(user)
    service = LanguagePreferenceService(
        user_repo=users, profile_repo=profile_repo, prompt_builder=prompt_builder,
        resolver=LanguageResolver(user_repo=users, account_repo=accounts, system_default_language=LanguageCode.EN),
    )
    return service, lambda: db.collection("users").docs[_USER_ID]


class TestSetPreferenceUserPersistence:
    """set_preference() persists updated config fields to UserRepository (asserted on the stored profile)."""

    async def test_agent_mirror_written_to_user_config(self, profile_repo, prompt_builder):
        service, stored = await _stored_service(
            profile_repo, prompt_builder, _make_user(preferred_language=LanguageCode.FR, agent_mirror=False))
        await service.set_preference(_USER_ID, preferred_language=None, agent_mirror=True)

        assert stored()["config"]["agent_mirror"] is True
        assert stored()["config"]["preferred_language"] is None

    async def test_agent_mirror_false_persisted(self, profile_repo, prompt_builder):
        service, stored = await _stored_service(profile_repo, prompt_builder, _make_user())
        await service.set_preference(_USER_ID, preferred_language=LanguageCode.FR, agent_mirror=False)

        assert stored()["config"]["agent_mirror"] is False
        assert stored()["config"]["preferred_language"] == LanguageCode.FR

    async def test_raises_if_user_not_found(self, service, user_repo):
        user_repo.get_user.return_value = None

        with pytest.raises(ValueError, match="User not found"):
            await service.set_preference(_USER_ID, preferred_language=None, agent_mirror=True)

    async def test_one_field_level_write(self, service, user_repo):
        """One write, field-level (owner decision O3): only the two language fields, never the whole profile."""
        await service.set_preference(_USER_ID, preferred_language=LanguageCode.EN, agent_mirror=False)

        user_repo.update_user_fields.assert_awaited_once()
        assert set(user_repo.update_user_fields.await_args[0][1]) == {"config.preferred_language", "config.agent_mirror"}
        user_repo.create_user.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tests: set_preference() — cache invalidation
# ---------------------------------------------------------------------------


class TestSetPreferenceCacheInvalidation:
    """set_preference() always invalidates the prompt cache."""

    async def test_cache_invalidated_on_fixed_language(self, service, prompt_builder):
        await service.set_preference(_USER_ID, preferred_language=LanguageCode.EN, agent_mirror=False)
        prompt_builder.invalidate_cache.assert_called_once()

    async def test_cache_invalidated_even_when_user_not_found(self, service, user_repo, prompt_builder):
        """ValueError is raised before cache invalidation — cache should NOT be touched."""
        user_repo.get_user.return_value = None

        with pytest.raises(ValueError):
            await service.set_preference(_USER_ID, preferred_language=None, agent_mirror=True)

        prompt_builder.invalidate_cache.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: set_preference() — notification dispatch
# ---------------------------------------------------------------------------


class TestSetPreferenceNotification:
    """set_preference() sends the correct system alert when notification_service is wired."""

    async def test_fixed_en_sends_en_alert(self, service_with_notify, notification_service):
        await service_with_notify.set_preference(_USER_ID, preferred_language=LanguageCode.EN, agent_mirror=False)

        alert = notification_service.notify.await_args[1]["system_alert"]
        assert alert == _LANG_ALERTS["en"]

    async def test_fixed_uk_sends_uk_alert(self, service_with_notify, notification_service):
        await service_with_notify.set_preference(_USER_ID, preferred_language=LanguageCode.UK, agent_mirror=False)

        alert = notification_service.notify.await_args[1]["system_alert"]
        assert alert == _LANG_ALERTS["uk"]

    async def test_fixed_fr_sends_fr_alert(self, service_with_notify, notification_service):
        await service_with_notify.set_preference(_USER_ID, preferred_language=LanguageCode.FR, agent_mirror=False)

        alert = notification_service.notify.await_args[1]["system_alert"]
        assert alert == _LANG_ALERTS["fr"]

    async def test_fixed_es_sends_es_alert(self, service_with_notify, notification_service):
        await service_with_notify.set_preference(_USER_ID, preferred_language=LanguageCode.ES, agent_mirror=False)

        alert = notification_service.notify.await_args[1]["system_alert"]
        assert alert == _LANG_ALERTS["es"]

    async def test_all_alerts_start_with_system_alert_prefix(self):
        for key, text in _LANG_ALERTS.items():
            assert text.startswith("System Alert:"), f"Alert '{key}' missing 'System Alert:' prefix"

    async def test_account_id_falls_back_to_user_id_when_missing(
        self, user_repo, resolver, profile_repo, prompt_builder, notification_service
    ):
        """User with no account_id → account_id in notify() falls back to user_id."""
        user_repo.get_user.return_value = _make_user(account_id=None)

        svc = LanguagePreferenceService(
            user_repo=user_repo,
            profile_repo=profile_repo,
            prompt_builder=prompt_builder,
            resolver=resolver,
            notification_service=notification_service,
        )

        await svc.set_preference(_USER_ID, preferred_language=LanguageCode.EN, agent_mirror=False)

        kwargs = notification_service.notify.await_args[1]
        assert kwargs["account_id"] == _USER_ID

    async def test_notification_failure_does_not_propagate(
        self, service_with_notify, notification_service
    ):
        """Notification errors are caught and logged — must not raise to the caller."""
        notification_service.notify.side_effect = RuntimeError("channel unavailable")

        # Should not raise
        await service_with_notify.set_preference(_USER_ID, preferred_language=LanguageCode.EN, agent_mirror=False)


# ---------------------------------------------------------------------------
# Tests: get_preference()
# ---------------------------------------------------------------------------


class TestGetPreference:
    """get_preference() returns (preferred_language, agent_mirror) from user config."""

    async def test_all_language_codes_round_trip(self, resolver, user_repo):
        for code in LanguageCode:
            user_repo.get_user.return_value = _make_user(preferred_language=code, agent_mirror=False)
            lang, _ = await resolver.get_preference(_USER_ID)
            assert lang == code


# ---------------------------------------------------------------------------
# Tests: resolve_ui_language() — 3-level resolution chain
# ---------------------------------------------------------------------------


class TestResolveUiLanguage:
    """resolve_ui_language() follows: user → account → system chain."""

    async def test_system_default_used_when_neither_user_nor_account_set(
        self, resolver, user_repo, account_repo
    ):
        user_repo.get_user.return_value = _make_user(preferred_language=None)
        account_repo.get_account.return_value = _make_account(default_language=None)

        result = await resolver.resolve_ui_language(_USER_ID)

        assert result == LanguageCode.EN  # system default in fixture

    async def test_system_default_used_when_user_has_no_account_id(
        self, resolver, user_repo, account_repo
    ):
        user_repo.get_user.return_value = _make_user(preferred_language=None, account_id=None)

        result = await resolver.resolve_ui_language(_USER_ID)

        assert result == LanguageCode.EN
        account_repo.get_account.assert_not_awaited()

    async def test_system_default_used_when_account_not_found(
        self, resolver, user_repo, account_repo
    ):
        user_repo.get_user.return_value = _make_user(preferred_language=None)
        account_repo.get_account.return_value = None

        result = await resolver.resolve_ui_language(_USER_ID)

        assert result == LanguageCode.EN

    async def test_user_preference_skips_account_lookup(
        self, resolver, user_repo, account_repo
    ):
        user_repo.get_user.return_value = _make_user(preferred_language=LanguageCode.ES)

        await resolver.resolve_ui_language(_USER_ID)

        account_repo.get_account.assert_not_awaited()
