"""Cross-side: a token the real `VoiceMcpTokenService` mints pairs with the real descriptors `voice_mcp_servers` builds
from the same config (LELIK_VOICE_MCP_RFC §4, §5.2). The mint's audience and the descriptors' URL are derived on two
sides from `MCP_RESOURCE_URI`; if either derivation drifts, the relay's audience binding drops the drive server and
every call silently loses its tools. Since the RFC §2 amendment (2026-11-10) the drive is the call's one server.

Two configs: the sealed deploy environment (`tests/support/prod_env.py`: the real `load_settings()` over the
`alek-bot-dev` step of `cloudbuild-dev.yaml`) and a plain config with an invented public base that needs canonicalising.
"""
import pytest

from src.adapters.firestore_ephemeral_store import FirestoreEphemeralStore
from src.composition import voice_mcp_setup
from src.config.auth import AuthConfig
from src.domain.mcp_resource import MCPResource
from src.domain.voice_mcp_servers import VOICE_MCP_SERVERS_FIELD, VOICE_MCP_TOKENS_FIELD, session_servers
from tests.support import prod_env
from tests.support.firestore_eval import EvalDb
from tests.support.mcp_gate_fakes import allowing_repos
from tests.support.voice_mcp_kit import ACCOUNT, COLLECTION, DIGEST_KEY, TICKET, USER, VOICE_KEY

XAI = {"provider": "xai", "turn_ownership": "provider", "voice": "castor", "reasoning_effort": "high"}


def _deploy_config(monkeypatch) -> dict:
    config, _ = prod_env.prod_settings(monkeypatch)
    return config


def _plain_config(monkeypatch) -> dict:
    return {"MCP_RESOURCE_URI": "HTTPS://Voice.Example.Test/mcp/", "OAUTH_SESSION_SECRET": "oauth-secret-" + "x" * 40}


@pytest.mark.parametrize("make_config", [
    # The real load_settings() over the deploy env takes over 0.5 s with coverage on.
    pytest.param(_deploy_config, id="sealed-deploy-env", marks=pytest.mark.slow),
    pytest.param(_plain_config, id="plain-config"),
])
async def test_a_minted_token_pairs_with_the_descriptors_built_from_the_same_config(monkeypatch, make_config):
    config = make_config(monkeypatch)
    monkeypatch.setenv(voice_mcp_setup.KNOB, "on")
    key = voice_mcp_setup.signing_key(config.get(voice_mcp_setup.KEY_NAME) or VOICE_KEY,
                                      oauth_secret=voice_mcp_setup.effective_oauth_secret(config), tools_enabled=True)
    user_repo, whitelist_repo = allowing_repos(USER)
    tokens_service = voice_mcp_setup.build_voice_mcp_tokens(
        key=key, store=FirestoreEphemeralStore(EvalDb(), collection=COLLECTION), digest_key=DIGEST_KEY,
        auth_config=AuthConfig(config), user_repo=user_repo, whitelist_repo=whitelist_repo, alert_sink=None)
    descriptors = voice_mcp_setup.voice_mcp_servers(config)
    assert tokens_service is not None and len(descriptors) == 1

    tokens = (await tokens_service.mint(TICKET, USER, ACCOUNT)).to_wire()
    servers = session_servers({
        "mode": "lelik", "voice": XAI,
        VOICE_MCP_SERVERS_FIELD: [server.to_dict() for server in descriptors],
        VOICE_MCP_TOKENS_FIELD: tokens,
    })

    assert [(server.label, server.token) for server in servers] == [("alek_drive", tokens[MCPResource.DRIVE.value])]
    # The paired token verifies at the resource its server's URL names.
    assert (await tokens_service.verify(servers[0].token, MCPResource.DRIVE)).user_id == USER
