"""The voice MCP token kind at both MCP resources (LELIK_VOICE_MCP_RFC §5.3-§5.4, §8 "Result cap", §11 S1).

- Routing: the facts provider (`MCPSdkOAuthProvider.load_access_token`) and the drive verifier
  (`DriveTokenVerifier.verify_token`) send a `voice-mcp` kid to the voice verifier only and anything else to the OAuth
  verifier only; each rejects the other's tokens, on key, type and audience, and a forged kid.
- The facts tool over HTTP: the tools claim, the voice cap, counts-only logging.
- The drive tools over HTTP on the production drive services (`drive_mcp_access_world`): the tools claim (neutral
  refusal, no read), the audience left unset (connector access "Nothing" still sees chat's view), the voice cap.

Real services throughout (`tests/support/voice_mcp_kit.py`); the facts search is a recording fake.
"""
import json
import logging
from types import SimpleNamespace
from typing import Any, List

import httpx
import pytest

from src.composition import drive_mcp_setup
from src.composition.drive_mcp_setup import DriveTokenVerifier, build_drive_mcp_app
from src.composition.mcp_sdk_oauth_provider import MCPSdkOAuthProvider
from src.composition.mcp_setup import build_mcp_components
from src.domain import drive_connector_contract as c
from src.domain.drive_connector_access import ConnectorAccess
from src.domain.mcp_resource import MCPResource
from src.domain.search import EnrichedContext, EnrichedFact
from src.domain.voice_mcp_token import VOICE_MCP_KID, VOICE_RESULT_MAX_CHARS, McpTokenKind
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.drive_connector_texts import marker
from tests.support.firestore_eval import EvalDb
from tests.support.voice_mcp_kit import (
    ACCOUNT,
    AUTH_CONFIG,
    DRIVE_URI,
    FACTS_URI,
    OAUTH_SECRET,
    TICKET,
    USER,
    VOICE_KEY,
    Kit,
    resign,
)

from .drive_mcp_access_world import NEUTRAL, PLAN_TEXT, World

MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
QUERY = "gate remotes"
FACT = "Garage space 14 behind 12 Oak Street"


@pytest.fixture
async def kit() -> Kit:
    return Kit()


@pytest.fixture
async def tokens(kit):
    return await kit.voice.mint(TICKET, USER, ACCOUNT)


def _facts_provider(kit: Kit, voice: Any = "kit") -> MCPSdkOAuthProvider:
    return MCPSdkOAuthProvider(kit.oauth, kit.voice if voice == "kit" else voice)


def _drive_verifier(kit: Kit, voice: Any = "kit") -> DriveTokenVerifier:
    return DriveTokenVerifier(kit.oauth, DRIVE_URI, kit.voice if voice == "kit" else voice)


# ----------------------------------------------------------------------------- routing and cross-rejection


class TestFactsResourceRouting:
    async def test_a_voice_token_loads_as_the_voice_kind_with_its_tools(self, kit, tokens):
        loaded = await _facts_provider(kit).load_access_token(tokens.facts)
        assert (loaded.kind, loaded.tools, loaded.user_id, loaded.account_id) == (
            McpTokenKind.VOICE, ["get_user_context"], USER, ACCOUNT)
        assert loaded.client_id == "voice" and loaded.scopes == ["user_context"] and loaded.grant_id is None

    async def test_an_oauth_token_loads_as_the_oauth_kind_and_reads_no_record(self, kit):
        loaded = await _facts_provider(kit).load_access_token(kit.oauth_token(MCPResource.FACTS))
        assert (loaded.kind, loaded.tools, loaded.user_id) == (McpTokenKind.OAUTH, None, USER)
        assert kit.store.reads == []

    async def test_the_drive_voice_token_is_refused_here(self, kit, tokens):
        assert await _facts_provider(kit).load_access_token(tokens.drive) is None

    async def test_a_voice_signed_token_shaped_as_oauth_is_refused(self, kit, tokens):
        """No voice kid, OAuth type and audience: it reaches the OAuth verifier, which refuses the key."""
        disguised = resign(tokens.facts, VOICE_KEY, header={}, type="mcp_access", aud=FACTS_URI, client_id="c")
        assert await _facts_provider(kit).load_access_token(disguised) is None

    async def test_an_oauth_token_with_a_forged_voice_kid_is_refused(self, kit):
        forged = resign(kit.oauth_token(MCPResource.FACTS), OAUTH_SECRET, header={"kid": VOICE_MCP_KID})
        assert await _facts_provider(kit).load_access_token(forged) is None

    async def test_an_oauth_signed_token_shaped_as_voice_is_refused(self, kit, tokens):
        disguised = resign(tokens.facts, OAUTH_SECRET)
        assert await _facts_provider(kit).load_access_token(disguised) is None

    async def test_a_voice_token_with_the_voice_kid_removed_is_refused_by_the_oauth_verifier(self, kit, tokens):
        assert await _facts_provider(kit).load_access_token(resign(tokens.facts, VOICE_KEY, header={})) is None

    async def test_without_a_voice_service_every_voice_token_is_refused(self, kit, tokens):
        assert await _facts_provider(kit, voice=None).load_access_token(tokens.facts) is None

    async def test_after_revoke_the_token_is_refused(self, kit, tokens):
        await kit.voice.revoke(TICKET)
        assert await _facts_provider(kit).load_access_token(tokens.facts) is None


class TestDriveResourceRouting:
    async def test_a_voice_token_verifies_as_the_voice_kind(self, kit, tokens):
        loaded = await _drive_verifier(kit).verify_token(tokens.drive)
        assert (loaded.kind, loaded.tools) == (McpTokenKind.VOICE, ["search_drive", "find_passages", "read_passages"])

    async def test_an_oauth_token_verifies_as_the_oauth_kind_and_reads_no_record(self, kit):
        loaded = await _drive_verifier(kit).verify_token(kit.oauth_token(MCPResource.DRIVE))
        assert (loaded.kind, loaded.tools, loaded.grant_id) == (McpTokenKind.OAUTH, None, "g-1")
        assert kit.store.reads == []

    async def test_the_facts_voice_token_is_refused_here(self, kit, tokens):
        assert await _drive_verifier(kit).verify_token(tokens.facts) is None

    async def test_a_voice_signed_token_shaped_as_oauth_is_refused(self, kit, tokens):
        disguised = resign(tokens.drive, VOICE_KEY, header={}, type="mcp_access", aud=DRIVE_URI, client_id="c")
        assert await _drive_verifier(kit).verify_token(disguised) is None

    async def test_an_oauth_token_with_a_forged_voice_kid_is_refused(self, kit):
        forged = resign(kit.oauth_token(MCPResource.DRIVE), OAUTH_SECRET, header={"kid": VOICE_MCP_KID})
        assert await _drive_verifier(kit).verify_token(forged) is None

    async def test_an_oauth_signed_token_shaped_as_voice_is_refused(self, kit, tokens):
        assert await _drive_verifier(kit).verify_token(resign(tokens.drive, OAUTH_SECRET)) is None

    async def test_without_a_voice_service_every_voice_token_is_refused(self, kit, tokens):
        assert await _drive_verifier(kit, voice=None).verify_token(tokens.drive) is None

    async def test_after_revoke_the_token_is_refused(self, kit, tokens):
        await kit.voice.revoke(TICKET)
        assert await _drive_verifier(kit).verify_token(tokens.drive) is None


# ----------------------------------------------------------------------------- the facts tool over HTTP


class RecordingSearch:
    """The facts search: answers `facts`, records every query it was asked."""

    def __init__(self, facts: List[EnrichedFact]) -> None:
        self.facts = facts
        self.queries: List[str] = []

    async def enrich_context(self, **kw: Any) -> EnrichedContext:
        self.queries.append(kw["search_phrase_1"])
        return EnrichedContext(facts=self.facts, total_sources=len(self.facts), dedup_count=0,
                               biographical_dedup_count=0)


FACTS_AUTH = SimpleNamespace(mcp_resource_uri=FACTS_URI, oauth_session_secret=OAUTH_SECRET, mcp_access_token_ttl=3600,
                             mcp_refresh_token_ttl=2592000, mcp_auth_code_ttl=600, mcp_consent_request_ttl=600)
ENV = SimpleNamespace(mcp_oauth_clients_collection="c", mcp_auth_codes_collection="a", mcp_refresh_tokens_collection="r")


class FactsApp:
    def __init__(self, kit: Kit, facts: List[EnrichedFact]) -> None:
        self.search = RecordingSearch(facts)
        components = build_mcp_components(
            db_client=EvalDb(), env_config=ENV, auth_config=FACTS_AUTH, search_enrichment_service=self.search,
            cipher=AesGcmTestCipher(), digest_key=bytes(range(32)), user_repo=kit.user_repo,
            whitelist_repo=kit.whitelist_repo, voice_tokens=kit.voice)
        # the components' own authorization service signs with the same OAuth secret as the kit's
        self.app = components.fastmcp

    async def call(self, token: str) -> httpx.Response:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "get_user_context", "arguments": {"query": QUERY}}}
        transport = httpx.ASGITransport(app=self.app.streamable_http_app(), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="https://mcp.example.test") as http:
            async with self.app.session_manager.run():
                return await http.post("/mcp", json=body, headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"})


def _text(resp: httpx.Response) -> str:
    assert resp.status_code == 200, resp.text
    data = next(line[len("data: "):] for line in resp.text.splitlines() if line.startswith("data: ")) \
        if resp.headers["content-type"].startswith("text/event-stream") else resp.text
    return json.loads(data)["result"]["content"][0]["text"]


def _fact(i: int, content: str = FACT) -> EnrichedFact:
    return EnrichedFact(fact_id=f"f{i}", content=content, source="keyword", domain="home")


class TestFactsTool:
    async def test_a_voice_token_reads_memory(self, kit, tokens):
        app = FactsApp(kit, [_fact(1)])
        assert FACT in _text(await app.call(tokens.facts))
        assert app.search.queries == [QUERY]

    async def test_a_tool_outside_the_claim_gets_the_neutral_refusal_and_no_read(self, kit, tokens):
        narrowed = resign(tokens.facts, VOICE_KEY, tools=["search_drive"])
        app = FactsApp(kit, [_fact(1)])
        text = _text(await app.call(narrowed))
        assert text == "Refused: this tool is not available to this caller."
        assert app.search.queries == []

    async def test_the_drive_voice_token_gets_401(self, kit, tokens):
        app = FactsApp(kit, [_fact(1)])
        assert (await app.call(tokens.drive)).status_code == 401
        assert app.search.queries == []

    async def test_a_revoked_voice_token_gets_401(self, kit, tokens):
        await kit.voice.revoke(TICKET)
        app = FactsApp(kit, [_fact(1)])
        assert (await app.call(tokens.facts)).status_code == 401

    async def test_a_voice_result_is_cut_at_the_voice_cap_and_an_oauth_one_is_not(self, kit, tokens):
        many = [_fact(i, f"{FACT}, note {i:04d} " + "x" * 80) for i in range(200)]
        voice_text = _text(await FactsApp(kit, many).call(tokens.facts))
        oauth_text = _text(await FactsApp(kit, many).call(kit.oauth_token(MCPResource.FACTS)))
        assert VOICE_RESULT_MAX_CHARS == 8000
        assert len(voice_text) <= VOICE_RESULT_MAX_CHARS < len(oauth_text)
        assert voice_text.endswith("ask a narrower question to see the rest.]") and oauth_text.startswith(voice_text
                                                                                                       .split("\n")[0])

    async def test_a_voice_call_logs_counts_never_the_query(self, kit, tokens, caplog):
        caplog.set_level(logging.DEBUG)
        await FactsApp(kit, [_fact(1)]).call(tokens.facts)
        lines = [r.getMessage() for r in caplog.records if "MCP get_user_context" in r.getMessage()]
        assert len(lines) == 2 and all("client=voice" in line for line in lines)
        assert f"query_chars={len(QUERY)}" in lines[0]
        # What reaches Cloud Logging (INFO and up): no query, no fact. At any level: no token.
        shipped = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.INFO)
        assert QUERY not in shipped and FACT not in shipped
        everything = "\n".join(r.getMessage() for r in caplog.records)
        assert tokens.facts not in everything and tokens.facts.split(".")[2] not in everything

    async def test_an_oauth_call_logs_as_before(self, kit, caplog):
        caplog.set_level(logging.DEBUG)
        await FactsApp(kit, [_fact(1)]).call(kit.oauth_token(MCPResource.FACTS))
        lines = [r.getMessage() for r in caplog.records if "MCP get_user_context" in r.getMessage()]
        assert f"query={QUERY!r}" in lines[0] and not any("client=voice" in line for line in lines)


# ----------------------------------------------------------------------------- the drive tools over HTTP


async def _drive_call(w: World, kit: Kit, token: str, name: str, **arguments: Any) -> str:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    app = build_drive_mcp_app(authorization_service=kit.oauth, auth_config=AUTH_CONFIG, tools=w.tools,
                              voice_tokens=kit.voice)
    transport = httpx.ASGITransport(app=app.streamable_http_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="https://mcp.example.test") as http:
        async with app.session_manager.run():
            resp = await http.post("/drive-mcp", json=body, headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"})
    return _text(resp)


@pytest.fixture
async def drive_world():
    """The access world's drive (user `u`) with connector access "Nothing", and a kit that allows `u`."""
    w = await World().build(connector=None)
    kit = Kit(users=("u",))
    tokens = await kit.voice.mint(TICKET, "u", "acc-1")
    return w, kit, tokens


class TestDriveTools:
    async def test_a_voice_token_sees_what_chat_sees_with_connector_access_nothing(self, drive_world):
        w, kit, tokens = drive_world
        search = await _drive_call(w, kit, tokens.drive, "search_drive", query="Tom's plan")
        passages = await _drive_call(w, kit, tokens.drive, "find_passages", question="What happens after the review?")
        read = await _drive_call(w, kit, tokens.drive, "read_passages", file_ref=w.ref(w.plan), passages="from 1")
        assert w.plan_path in search
        assert PLAN_TEXT in passages and PLAN_TEXT in read

    async def test_an_oauth_token_runs_under_the_connector_audience(self, drive_world):
        w, kit, _ = drive_world
        token = kit.oauth_token(MCPResource.DRIVE, user_id="u")
        search = await _drive_call(w, kit, token, "search_drive", query="Tom's plan")
        read = await _drive_call(w, kit, token, "read_passages", file_ref=w.ref(w.plan), passages="from 1")
        note = await _drive_call(w, kit, token, "get_note", item_ref=w.ref(w.plan))
        assert w.plan_path not in search and PLAN_TEXT not in read
        assert read.split("\n") == NEUTRAL and note.split("\n") == NEUTRAL
        assert w.notes.audiences == []          # refused by the connector's access before the note store

    @pytest.mark.parametrize("name, arguments", [
        pytest.param("open_drive_file", {"file_ref": "drive:a1:x"}, id="open"),
        pytest.param("list_folder", {"folder": "/"}, id="list"),
        pytest.param("get_note", {"item_ref": "drive:a1:x"}, id="note"),
        pytest.param("get_download_link", {"file_ref": "drive:a1:x"}, id="link"),
    ])
    async def test_a_tool_outside_the_claim_is_the_neutral_refusal_with_no_read(self, drive_world, name, arguments):
        w, kit, tokens = drive_world
        if "file_ref" in arguments or "item_ref" in arguments:
            arguments = {k: w.ref(w.plan) for k in arguments}
        text = await _drive_call(w, kit, tokens.drive, name, **arguments)
        assert text.split("\n") == NEUTRAL
        assert w.conversion.opened == [] and w.notes.reads == []

    async def test_the_facts_voice_token_gets_401_on_the_drive(self, drive_world):
        w, kit, tokens = drive_world
        app = build_drive_mcp_app(authorization_service=kit.oauth, auth_config=AUTH_CONFIG, tools=w.tools,
                                  voice_tokens=kit.voice)
        transport = httpx.ASGITransport(app=app.streamable_http_app(), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="https://mcp.example.test") as http:
            async with app.session_manager.run():
                resp = await http.post("/drive-mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                       headers={**MCP_HEADERS, "Authorization": f"Bearer {tokens.facts}"})
        assert resp.status_code == 401

    async def test_a_voice_result_is_cut_at_the_voice_cap_by_token_kind(self, drive_world, monkeypatch):
        w, kit, tokens = drive_world
        whole = await World().build(connector=ConnectorAccess.whole_drive())
        oauth_token = kit.oauth_token(MCPResource.DRIVE, user_id="u")
        args = {"file_ref": w.ref(w.plan), "passages": "from 1"}
        uncut = await _drive_call(w, kit, tokens.drive, "read_passages", **args)
        assert len(uncut) > 120 and not uncut.endswith(marker(c.OVER_SIZE))
        monkeypatch.setattr(drive_mcp_setup, "VOICE_RESULT_MAX_CHARS", 120)
        voice = await _drive_call(w, kit, tokens.drive, "read_passages", **args)
        oauth = await _drive_call(whole, kit, oauth_token, "read_passages", file_ref=whole.ref(whole.plan),
                                  passages="from 1")
        assert len(voice) <= 120 and voice.endswith(marker(c.OVER_SIZE))
        assert PLAN_TEXT in oauth and len(oauth) > 120 and not oauth.endswith(marker(c.OVER_SIZE))

    async def test_a_voice_call_logs_client_voice_and_no_token(self, drive_world, caplog):
        w, kit, tokens = drive_world
        caplog.set_level(logging.DEBUG)
        await _drive_call(w, kit, tokens.drive, "search_drive", query="Tom's plan")
        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[DriveMCP] tool=search_drive")]
        assert len(lines) == 1 and lines[0].endswith(" client=voice")
        everything = "\n".join(r.getMessage() for r in caplog.records)
        assert tokens.drive not in everything and tokens.drive.split(".")[2] not in everything
        assert "Tom's plan" not in everything


def test_the_voice_cap_is_the_rfc_starting_point():
    assert VOICE_RESULT_MAX_CHARS == 8000 and drive_mcp_setup.RESULT_MAX_CHARS == 100_000

