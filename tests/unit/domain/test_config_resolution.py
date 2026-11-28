"""The override rule (src/domain/config_resolution.py): per key the user's value, else the account's, else the SYSTEM
default; a key the SYSTEM level does not declare is not a setting."""
import pytest

from src.domain.config_resolution import SystemConfig, resolve
from src.domain.user import UserBotConfig

SYSTEM = SystemConfig(intent_access={"tutor_chat": True, "generate_image": True, "generate_video": False})


def level(**access: bool) -> UserBotConfig:
    return UserBotConfig(intent_access=access)


class TestResolve:

    def test_without_levels_every_key_takes_the_system_default(self):
        assert dict(resolve(SYSTEM, None, None).intent_access) == {
            "tutor_chat": True, "generate_image": True, "generate_video": False}

    def test_empty_levels_inherit_the_system_default(self):
        assert dict(resolve(SYSTEM, level(), level()).intent_access) == dict(SYSTEM.intent_access)

    def test_the_user_wins_over_the_account_and_the_account_over_the_system_per_key(self):
        account = level(tutor_chat=False, generate_image=False, generate_video=True)
        user = level(tutor_chat=True)

        got = resolve(SYSTEM, account, user).intent_access

        assert dict(got) == {"tutor_chat": True, "generate_image": False, "generate_video": True}

    def test_a_key_only_the_account_sets_applies_to_a_user_without_it(self):
        got = resolve(SYSTEM, level(generate_image=False), level(tutor_chat=True)).intent_access
        assert got["generate_image"] is False and got["tutor_chat"] is True

    def test_the_user_level_alone_applies_without_an_account_level(self):
        got = resolve(SYSTEM, None, level(generate_video=True, tutor_chat=False)).intent_access
        assert dict(got) == {"tutor_chat": False, "generate_image": True, "generate_video": True}

    def test_a_key_the_system_level_does_not_declare_is_ignored_at_both_levels(self):
        got = resolve(SYSTEM, level(ask_alek=False), level(generate_docx_code=False)).intent_access
        assert set(got) == {"tutor_chat", "generate_image", "generate_video"}


class TestEffectiveConfig:

    def test_an_ungated_intent_is_available_and_a_gated_one_follows_its_value(self):
        effective = resolve(SYSTEM, None, level(tutor_chat=False))
        assert effective.is_intent_available("ask_alek") is True
        assert effective.is_intent_available("tutor_chat") is False
        assert effective.is_intent_available("generate_image") is True

    def test_the_resolved_map_is_read_only_and_detached_from_the_levels(self):
        user = level(tutor_chat=False)
        effective = resolve(SYSTEM, None, user)

        with pytest.raises(TypeError):
            effective.intent_access["tutor_chat"] = True  # type: ignore[index]
        user.intent_access["tutor_chat"] = True
        assert effective.is_intent_available("tutor_chat") is False

    def test_the_system_level_is_read_only_and_detached_from_its_source(self):
        source = {"tutor_chat": True}
        system = SystemConfig(intent_access=source)
        source["tutor_chat"] = False

        with pytest.raises(TypeError):
            system.intent_access["tutor_chat"] = False  # type: ignore[index]
        assert resolve(system, None, None).is_intent_available("tutor_chat") is True
