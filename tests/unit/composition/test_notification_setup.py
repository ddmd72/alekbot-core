"""`build_notifications`: the notification service holds Slack's one kit (media port, rich content over the media bucket and
the renderer, channel maker on the one Slack app) and the given coordinator, session store and language services."""
import dataclasses
from types import SimpleNamespace

import pytest

from src.adapters.slack.response_channel import SlackResponseChannel
from src.composition import notification_setup
from src.composition.notification_setup import build_notifications
from src.config.environment import EnvironmentConfig
from src.infrastructure.notification_sla import NOTIFICATION_SLA
from tests.support.firestore_eval import EvalDb

COORDINATOR, SESSIONS, MEDIA, LINKS = object(), object(), object(), object()
LANGUAGES = SimpleNamespace(localization=object(), resolver=object())


class _Renderer:
    pass


@pytest.fixture
def build(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setattr(notification_setup, "PlaywrightHtmlRenderer", _Renderer)
    env = EnvironmentConfig()

    def run(**config):
        return build_notifications({"SLACK_BOT_TOKEN": "xoxb-test-1", **config}, EvalDb(), env, coordinator=COORDINATOR,
                                   session_store=SESSIONS, media_storage=MEDIA, link_service=LINKS, languages=LANGUAGES)  # type: ignore[arg-type]
    return run


def test_the_service_gets_slacks_one_kit_and_the_given_collaborators(build):
    built = build()

    service, kit = built.service, built.slack_kit
    assert service._state_repo is built.state_repo and service._channel_factory is built.channel_factory
    assert service._coordinator is COORDINATOR and service._session_store is SESSIONS
    assert service._notification_sla is NOTIFICATION_SLA
    assert service._localization is LANGUAGES.localization and service._language is LANGUAGES.resolver
    assert service._platform_media == {"slack": kit.media} and service._platform_rich_content == {"slack": kit.rich_content}
    assert built.channel_factory._factories == {"slack": kit.channel}
    assert kit.media._client is built.slack_app.client and kit.media._bot_token == "xoxb-test-1"
    assert kit.rich_content._media_port is kit.media and kit.rich_content._storage_port is MEDIA


def test_the_channel_maker_posts_through_the_one_slack_app_with_the_bot_token(build):
    built = build()

    channel = built.slack_kit.channel("C-1")

    assert isinstance(channel, SlackResponseChannel)
    assert (channel.client, channel.channel_id, channel.bot_token) == (built.slack_app.client, "C-1", "xoxb-test-1")


def test_the_renderer_is_built_only_with_its_switch_and_the_slack_kit_renders_with_it(build):
    on = build(ENABLE_HTML_RENDERER="1")
    off = build()

    assert isinstance(on.html_renderer, _Renderer) and on.slack_kit.rich_content._html_renderer is on.html_renderer
    assert off.html_renderer is None and off.slack_kit.rich_content._html_renderer is None


def test_the_bundle_is_frozen(build):
    with pytest.raises(dataclasses.FrozenInstanceError):
        build().service = None  # type: ignore[misc]


def test_slacks_rich_content_links_html_pages_through_the_given_link_service(build):
    """P11-G4-5 (O8): Slack's kit stores an html page in the media bucket and links it through `/f/<token>`."""
    kit = build().slack_kit
    assert kit.rich_content._storage_port is MEDIA and kit.rich_content._link_service is LINKS


def test_no_part_of_the_bot_token_is_logged(build, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    build()
    assert "xoxb-test" not in caplog.text
