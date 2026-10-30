"""The production application graph passes the values production gives it (P9 T5b, the wiring mutants no earlier test killed).

Two graphs are read:

- `prod_graph`, the deploy's sealed environment as is: the account collection, the agents the coordinator holds, the
  deep-research providers, and the channels the notification service builds on demand.
- `variant_graph`, the same `build_application_graph(config, db)` with three differences that a deploy can legitimately
  have: the SDK-fallback environment variables are absent (a secret that came from Secret Manager is in `config` but
  never in `os.environ`, and the OpenAI, Anthropic, Twilio and Slack SDKs all read their own variable when handed
  `None`), the optional model pins are set, and the consolidation settings are not the dataclass default. Without that,
  a graph that dropped a key would still work by accident of the SDK, and the values below would look the same.

Nothing here asserts a call or a log line: each check reads the attribute that holds the value in the object production uses.
"""
import os
from typing import Iterator, NamedTuple

import pytest

from src.domain.settings import ConsolidationSettings
from tests.support import prod_env
from tests.support.prod_graph import ProdGraph, prod_graph  # noqa: F401  (the fixture)

# What a deploy that took its secrets from Secret Manager would not have in the process environment.
SDK_FALLBACK_VARIABLES = (
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "SLACK_BOT_TOKEN",
    "FIREBASE_WEB_API_KEY", "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "OAUTH_SESSION_SECRET",
)
MODEL_PINS = {
    "OPENAI_TRANSCRIPTION_MODEL": "pinned-transcription-model",
    "OPENAI_DEEP_RESEARCH_MODEL": "pinned-openai-research-model",
    "CLAUDE_DEEP_RESEARCH_MODEL": "pinned-claude-research-model",
}
NON_DEFAULT_CONSOLIDATION = ConsolidationSettings(threshold=33, batch_size=11, max_queue_messages=222, max_retry_attempts=7)


class VariantGraph(NamedTuple):
    app: object
    config: dict


@pytest.fixture
def variant_graph(monkeypatch) -> Iterator[VariantGraph]:
    from src.composition.application_graph import build_application_graph
    from src.config.settings import load_settings

    offline = prod_env.install_offline_services(monkeypatch)
    config = load_settings()
    for name, value in MODEL_PINS.items():
        monkeypatch.setenv(name, value)
    for name in SDK_FALLBACK_VARIABLES:
        monkeypatch.delenv(name)
    config["CONSOLIDATION"] = NON_DEFAULT_CONSOLIDATION
    yield VariantGraph(build_application_graph(config, offline.db), config)


# ----------------------------------------------------------------------------- the deploy's own environment

@pytest.mark.slow
async def test_accounts_are_stored_in_the_environments_account_collection(prod_graph):
    app = prod_graph.app
    name = app.agent_factory.env_config.account_collection_name

    assert app.account_repo.accounts_collection.name == name
    assert "a1" in prod_graph.offline.db.collection(name).docs


@pytest.mark.slow
async def test_the_coordinator_holds_the_billing_and_the_logger_agent(prod_graph):
    app = prod_graph.app

    assert app.coordinator.get_agent("billing_agent") is app.billing_agent
    assert app.coordinator.get_agent("logger_agent") is app.logger_agent


@pytest.mark.slow
async def test_both_deep_research_providers_are_registered_under_the_deploys_keys(prod_graph):
    registry = prod_graph.app.agent_factory.job_registry

    assert sorted(registry.list_available()) == ["claude", "openai"]


@pytest.mark.slow
async def test_the_notification_channels_carry_the_adapters_client_token_and_bot(prod_graph):
    app = prod_graph.app
    factory = app.notification_service._channel_factory
    slack = factory.create("slack", "C123")
    telegram = factory.create("telegram", "4242")

    assert slack.client is app.slack_adapter.app.client
    assert slack.channel_id == "C123"
    assert slack.bot_token == prod_graph.offline.env["SLACK_BOT_TOKEN"]
    assert telegram.bot is app.telegram_adapter.bot
    assert telegram.chat_id == 4242
    assert telegram.channel_id == "4242"


@pytest.mark.slow
async def test_the_firebase_adapter_holds_the_google_oauth_client_of_the_deploy(prod_graph):
    firebase = prod_graph.app.auth_registry.get_provider("firebase")
    env = prod_graph.offline.env

    assert firebase.oauth_client_id == env["GOOGLE_OAUTH_CLIENT_ID"]
    assert firebase.oauth_client_secret == env["GOOGLE_OAUTH_CLIENT_SECRET"]
    assert firebase.web_api_key == env["FIREBASE_WEB_API_KEY"]


# ----------------------------------------------------------------------------- secrets that reach the clients through config

@pytest.mark.slow
def test_the_openai_clients_hold_the_configured_key_and_the_pinned_models(variant_graph):
    app, config = variant_graph
    audio = app.slack_adapter.conversation_handler.audio_service
    openai_research = app.agent_factory.job_registry.get("openai")
    claude_research = app.agent_factory.job_registry.get("claude")

    assert audio.client.api_key == config["OPENAI_API_KEY"]
    assert audio.model == MODEL_PINS["OPENAI_TRANSCRIPTION_MODEL"]
    assert openai_research._client.api_key == config["OPENAI_API_KEY"]
    assert openai_research._model_override == MODEL_PINS["OPENAI_DEEP_RESEARCH_MODEL"]
    assert claude_research._model_override == MODEL_PINS["CLAUDE_DEEP_RESEARCH_MODEL"]


@pytest.mark.slow
def test_the_anthropic_client_holds_the_configured_key(variant_graph):
    app, config = variant_graph

    assert app.agent_factory.anthropic_client.api_key == config["ANTHROPIC_API_KEY"]


@pytest.mark.slow
def test_the_auth_services_hold_the_configured_values(variant_graph):
    app, config = variant_graph
    auth = app.auth_config

    assert auth.firebase_web_api_key == config["FIREBASE_WEB_API_KEY"]
    assert auth.google_oauth_client_id == config["GOOGLE_OAUTH_CLIENT_ID"]
    assert auth.google_oauth_client_secret == config["GOOGLE_OAUTH_CLIENT_SECRET"]
    assert auth.oauth_session_secret == config["OAUTH_SESSION_SECRET"]
    firebase = app.auth_registry.get_provider("firebase")
    assert firebase.web_api_key == config["FIREBASE_WEB_API_KEY"]
    assert firebase.oauth_client_id == config["GOOGLE_OAUTH_CLIENT_ID"]
    assert firebase.oauth_client_secret == config["GOOGLE_OAUTH_CLIENT_SECRET"]
    # the Google connect edge replaced GoogleOAuthService (MULTI_DRIVE_RFC §5.1); the connect service holds it
    google = app.container.connectors.realm_edge("google")
    assert google._client_id == config["GOOGLE_OAUTH_CLIENT_ID"]
    assert google._client_secret == config["GOOGLE_OAUTH_CLIENT_SECRET"]
    assert app.connect_service._edges["google"] is google
    # the one connect callback (MULTI_DRIVE_RFC §5.2, E4) reaches the connect service
    assert auth.oauth_connect_redirect_uri == prod_env.SECRET_VALUES["OAUTH_CONNECT_REDIRECT_URI"]
    assert app.connect_service._redirect_uri == auth.oauth_connect_redirect_uri


@pytest.mark.slow
def test_the_twilio_verify_client_holds_the_configured_credentials(variant_graph):
    app, config = variant_graph
    twilio = app.twilio_verify_client._version.domain.twilio

    assert twilio.username == config["TWILIO_ACCOUNT_SID"]
    assert twilio.password == config["TWILIO_AUTH_TOKEN"]


@pytest.mark.slow
def test_the_slack_app_holds_the_configured_bot_token(variant_graph):
    app, config = variant_graph

    assert app.slack_adapter.app.client.token == config["SLACK_BOT_TOKEN"]
    assert app.slack_adapter.slack_bot_token == config["SLACK_BOT_TOKEN"]


@pytest.mark.slow
def test_both_platform_handlers_hold_the_configured_consolidation_settings(variant_graph):
    app, _ = variant_graph

    assert app.slack_adapter.conversation_handler.global_config == NON_DEFAULT_CONSOLIDATION
    assert app.telegram_adapter.conversation_handler.global_config == NON_DEFAULT_CONSOLIDATION
