"""Security-review hardening of the voice MCP token kind (LELIK_VOICE_MCP_RFC §5.3): the tools are the server's set
narrowed by the claim, and the tenant comes from the server's live-call record, not from the token alone."""
import pytest

from src.domain.mcp_resource import MCPResource
from src.domain.voice_mcp_token import McpTokenKind, tool_permitted, voice_call_id, voice_call_key
from tests.support.voice_mcp_kit import ACCOUNT, DIGEST_KEY, TICKET, USER, VOICE_KEY, Kit, resign

CALL_KEY = voice_call_key(voice_call_id(DIGEST_KEY, TICKET))


@pytest.fixture
async def minted():
    kit = Kit()
    return kit, await kit.voice.mint(TICKET, USER, ACCOUNT)


class TestToolsAreTheServerSet:
    async def test_a_claimed_extra_tool_is_dropped_and_refused(self, minted):
        kit, tokens = minted
        widened = resign(tokens.drive, VOICE_KEY, tools=["search_drive", "find_passages", "read_passages",
                                                         "open_drive_file", "get_download_link"])
        verified = await kit.voice.verify(widened, MCPResource.DRIVE)
        assert verified.tools == ["search_drive", "find_passages", "read_passages"]
        assert not tool_permitted(McpTokenKind.VOICE, verified.tools, "open_drive_file")
        assert not tool_permitted(McpTokenKind.VOICE, verified.tools, "get_download_link")

    async def test_a_drive_token_claiming_the_facts_tool_gets_only_drive_tools(self, minted):
        kit, tokens = minted
        widened = resign(tokens.drive, VOICE_KEY, tools=["search_drive", "get_user_context"])
        verified = await kit.voice.verify(widened, MCPResource.DRIVE)
        assert verified.tools == ["search_drive"]
        assert not tool_permitted(McpTokenKind.VOICE, verified.tools, "get_user_context")

    async def test_a_claim_can_narrow_the_set(self, minted):
        kit, tokens = minted
        narrowed = resign(tokens.drive, VOICE_KEY, tools=["read_passages"])
        assert (await kit.voice.verify(narrowed, MCPResource.DRIVE)).tools == ["read_passages"]


class TestTenantComesFromTheRecord:
    async def test_mint_writes_the_account_into_the_live_call_record(self, minted):
        kit, _ = minted
        assert kit.docs[CALL_KEY]["value"] == {"user_id": USER, "account_id": ACCOUNT}

    async def test_a_token_whose_account_differs_from_the_record_is_refused(self, minted):
        kit, tokens = minted
        assert await kit.voice.verify(resign(tokens.drive, VOICE_KEY, account_id="acc-2"), MCPResource.DRIVE) is None

    async def test_a_record_without_an_account_refuses(self, minted):
        kit, tokens = minted
        await kit.store.inner.set(CALL_KEY, {"user_id": USER}, ttl_s=60)
        assert await kit.voice.verify(tokens.drive, MCPResource.DRIVE) is None

    async def test_the_minted_token_still_verifies(self, minted):
        kit, tokens = minted
        assert (await kit.voice.verify(tokens.drive, MCPResource.DRIVE)).account_id == ACCOUNT
