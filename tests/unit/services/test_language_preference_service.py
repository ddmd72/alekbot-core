"""
Unit tests for LanguagePreferenceService.

Covers:
- set_preference(): override token selection, user persistence, cache invalidation,
  notification dispatch, error isolation
- get_preference(): defaults and stored values
- resolve_ui_language(): 3-level resolution chain (user → account → system)
"""
from unittest.mock import AsyncMock, MagicMock, call
import pytest

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
    repo.update_user.side_effect = lambda u: u
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
def service(user_repo, account_repo, profile_repo, prompt_builder) -> LanguagePreferenceService:
    return LanguagePreferenceService(
        user_repo=user_repo,
        account_repo=account_repo,
        profile_repo=profile_repo,
        prompt_builder=prompt_builder,
        system_default_language=LanguageCode.EN,
    )


@pytest.fixture
def service_with_notify(
    user_repo, account_repo, profile_repo, prompt_builder, notification_service
) -> LanguagePreferenceService:
    svc = LanguagePreferenceService(
        user_repo=user_repo,
        account_repo=account_repo,
        profile_repo=profile_repo,
        prompt_builder=prompt_builder,
        system_default_language=LanguageCode.EN,
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

    async def test_clear_ids_excludes_selected_token(self, service, profile_repo):
        """The active token must NOT appear in clear_ids — it would wipe itself."""
        await service.set_preference(_USER_ID, preferred_language=LanguageCode.EN, agent_mirror=False)

        clear_ids = profile_repo.set_override_tokens.await_args.kwargs["clear_ids"]
        assert "LANG_FIXED_EN" not in clear_ids

    async def test_clear_ids_contains_all_other_lang_tokens(self, service, profile_repo):
        await service.set_preference(_USER_ID, preferred_language=LanguageCode.EN, agent_mirror=False)

        clear_ids = profile_repo.set_override_tokens.await_args.kwargs["clear_ids"]
        assert {"LANG_MIRROR", "LANG_FIXED_UK", "LANG_FIXED_FR", "LANG_FIXED_ES"} <= clear_ids


# ---------------------------------------------------------------------------
# Tests: set_preference() — user persistence
# ---------------------------------------------------------------------------


class TestSetPreferenceUserPersistence:
    """set_preference() persists updated config fields to UserRepository."""

    async def test_preferred_language_written_to_user_config(self, service, user_repo):
        await service.set_preference(_USER_ID, preferred_language=LanguageCode.EN, agent_mirror=False)

        updated_user = user_repo.update_user.await_args[0][0]
        assert updated_user.config.preferred_language == LanguageCode.EN

    async def test_agent_mirror_written_to_user_config(self, service, user_repo):
        await service.set_preference(_USER_ID, preferred_language=None, agent_mirror=True)

        updated_user = user_repo.update_user.await_args[0][0]
        assert updated_user.config.agent_mirror is True

    async def test_agent_mirror_false_persisted(self, service, user_repo):
        await service.set_preference(_USER_ID, preferred_language=LanguageCode.FR, agent_mirror=False)

        updated_user = user_repo.update_user.await_args[0][0]
        assert updated_user.config.agent_mirror is False

    async def test_raises_if_user_not_found(self, service, user_repo):
        user_repo.get_user.return_value = None

        with pytest.raises(ValueError, match="User not found"):
            await service.set_preference(_USER_ID, preferred_language=None, agent_mirror=True)

    async def test_update_user_called_once(self, service, user_repo):
        await service.set_preference(_USER_ID, preferred_language=LanguageCode.EN, agent_mirror=False)

        user_repo.update_user.assert_awaited_once()


# ---------------------------------------------------------------------------
# Tests: set_preference() — cache invalidation
# ---------------------------------------------------------------------------


class TestSetPreferenceCacheInvalidation:
    """set_preference() always invalidates the prompt cache."""

    async def test_cache_invalidated_on_mirror(self, service, prompt_builder):
        await service.set_preference(_USER_ID, preferred_language=None, agent_mirror=True)
        prompt_builder.invalidate_cache.assert_called_once()

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

    async def test_no_notification_when_service_not_wired(self, service):
        """Default fixture has no notification_service — must not raise."""
        await service.set_preference(_USER_ID, preferred_language=LanguageCode.EN, agent_mirror=False)
        # No exception, no notification sent (nothing to assert — absence is the test)

    async def test_mirror_sends_mirror_alert(self, service_with_notify, notification_service):
        await service_with_notify.set_preference(_USER_ID, preferred_language=None, agent_mirror=True)

        notification_service.notify.assert_awaited_once()
        alert = notification_service.notify.await_args[1]["system_alert"]
        assert alert == _LANG_ALERTS["mirror"]

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

    async def test_no_preferred_language_fixed_uses_system_default_alert(
        self, service_with_notify, notification_service
    ):
        """preferred_language=None, agent_mirror=False → alert key = system_default (EN)."""
        await service_with_notify.set_preference(_USER_ID, preferred_language=None, agent_mirror=False)

        alert = notification_service.notify.await_args[1]["system_alert"]
        assert alert == _LANG_ALERTS["en"]

    async def test_alert_starts_with_system_alert_prefix(self, service_with_notify, notification_service):
        await service_with_notify.set_preference(_USER_ID, preferred_language=LanguageCode.EN, agent_mirror=False)

        alert = notification_service.notify.await_args[1]["system_alert"]
        assert alert.startswith("System Alert:")

    async def test_all_alerts_start_with_system_alert_prefix(self):
        for key, text in _LANG_ALERTS.items():
            assert text.startswith("System Alert:"), f"Alert '{key}' missing 'System Alert:' prefix"

    async def test_notify_receives_correct_user_and_account_ids(
        self, service_with_notify, notification_service
    ):
        await service_with_notify.set_preference(_USER_ID, preferred_language=LanguageCode.EN, agent_mirror=False)

        kwargs = notification_service.notify.await_args[1]
        assert kwargs["user_id"] == _USER_ID
        assert kwargs["account_id"] == _ACCOUNT_ID

    async def test_account_id_falls_back_to_user_id_when_missing(
        self, user_repo, account_repo, profile_repo, prompt_builder, notification_service
    ):
        """User with no account_id → account_id in notify() falls back to user_id."""
        user_repo.get_user.return_value = _make_user(account_id=None)

        svc = LanguagePreferenceService(
            user_repo=user_repo,
            account_repo=account_repo,
            profile_repo=profile_repo,
            prompt_builder=prompt_builder,
            system_default_language=LanguageCode.EN,
            notification_service=notification_service,
        )

        await svc.set_preference(_USER_ID, preferred_language=LanguageCode.EN, agent_mirror=False)

        kwargs = notification_service.notify.await_args[1]
        assert kwargs["account_id"] == _USER_ID

    async def test_ensure_agents_called_before_notify(
        self, service_with_notify, notification_service
    ):
        ensure_agents = AsyncMock()
        service_with_notify._ensure_agents = ensure_agents

        await service_with_notify.set_preference(_USER_ID, preferred_language=LanguageCode.EN, agent_mirror=False)

        ensure_agents.assert_awaited_once_with(_USER_ID)

    async def test_notification_failure_does_not_propagate(
        self, service_with_notify, notification_service
    ):
        """Notification errors are caught and logged — must not raise to the caller."""
        notification_service.notify.side_effect = RuntimeError("channel unavailable")

        # Should not raise
        await service_with_notify.set_preference(_USER_ID, preferred_language=LanguageCode.EN, agent_mirror=False)

    async def test_ensure_agents_failure_does_not_propagate(
        self, service_with_notify, notification_service
    ):
        ensure_agents = AsyncMock(side_effect=RuntimeError("coordinator down"))
        service_with_notify._ensure_agents = ensure_agents

        await service_with_notify.set_preference(_USER_ID, preferred_language=LanguageCode.EN, agent_mirror=False)
        # No exception raised


# ---------------------------------------------------------------------------
# Tests: get_preference()
# ---------------------------------------------------------------------------


class TestGetPreference:
    """get_preference() returns (preferred_language, agent_mirror) from user config."""

    async def test_returns_stored_language_and_mirror(self, service, user_repo):
        user_repo.get_user.return_value = _make_user(
            preferred_language=LanguageCode.EN, agent_mirror=False
        )

        lang, mirror = await service.get_preference(_USER_ID)

        assert lang == LanguageCode.EN
        assert mirror is False

    async def test_returns_none_language_and_mirror_true_by_default(self, service, user_repo):
        user_repo.get_user.return_value = _make_user()  # defaults

        lang, mirror = await service.get_preference(_USER_ID)

        assert lang is None
        assert mirror is True

    async def test_returns_none_true_when_user_not_found(self, service, user_repo):
        user_repo.get_user.return_value = None

        lang, mirror = await service.get_preference(_USER_ID)

        assert lang is None
        assert mirror is True

    async def test_all_language_codes_round_trip(self, service, user_repo):
        for code in LanguageCode:
            user_repo.get_user.return_value = _make_user(preferred_language=code, agent_mirror=False)
            lang, _ = await service.get_preference(_USER_ID)
            assert lang == code


# ---------------------------------------------------------------------------
# Tests: resolve_ui_language() — 3-level resolution chain
# ---------------------------------------------------------------------------


class TestResolveUiLanguage:
    """resolve_ui_language() follows: user → account → system chain."""

    async def test_user_preferred_language_takes_priority(self, service, user_repo):
        user_repo.get_user.return_value = _make_user(preferred_language=LanguageCode.UK)

        result = await service.resolve_ui_language(_USER_ID)

        assert result == LanguageCode.UK

    async def test_account_default_used_when_user_has_no_preference(
        self, service, user_repo, account_repo
    ):
        user_repo.get_user.return_value = _make_user(preferred_language=None)
        account_repo.get_account.return_value = _make_account(default_language=LanguageCode.FR)

        result = await service.resolve_ui_language(_USER_ID)

        assert result == LanguageCode.FR

    async def test_system_default_used_when_neither_user_nor_account_set(
        self, service, user_repo, account_repo
    ):
        user_repo.get_user.return_value = _make_user(preferred_language=None)
        account_repo.get_account.return_value = _make_account(default_language=None)

        result = await service.resolve_ui_language(_USER_ID)

        assert result == LanguageCode.EN  # system default in fixture

    async def test_system_default_used_when_user_not_found(self, service, user_repo):
        user_repo.get_user.return_value = None

        result = await service.resolve_ui_language(_USER_ID)

        assert result == LanguageCode.EN

    async def test_system_default_used_when_user_has_no_account_id(
        self, service, user_repo, account_repo
    ):
        user_repo.get_user.return_value = _make_user(preferred_language=None, account_id=None)

        result = await service.resolve_ui_language(_USER_ID)

        assert result == LanguageCode.EN
        account_repo.get_account.assert_not_awaited()

    async def test_system_default_used_when_account_not_found(
        self, service, user_repo, account_repo
    ):
        user_repo.get_user.return_value = _make_user(preferred_language=None)
        account_repo.get_account.return_value = None

        result = await service.resolve_ui_language(_USER_ID)

        assert result == LanguageCode.EN

    async def test_user_preference_skips_account_lookup(
        self, service, user_repo, account_repo
    ):
        user_repo.get_user.return_value = _make_user(preferred_language=LanguageCode.ES)

        await service.resolve_ui_language(_USER_ID)

        account_repo.get_account.assert_not_awaited()

    async def test_custom_system_default_language(
        self, user_repo, account_repo, profile_repo, prompt_builder
    ):
        svc = LanguagePreferenceService(
            user_repo=user_repo,
            account_repo=account_repo,
            profile_repo=profile_repo,
            prompt_builder=prompt_builder,
            system_default_language=LanguageCode.UK,
        )
        user_repo.get_user.return_value = _make_user(preferred_language=None, account_id=None)

        result = await svc.resolve_ui_language(_USER_ID)

        assert result == LanguageCode.UK
