"""The voice MCP token kind: mint, verify, revoke (LELIK_VOICE_MCP_RFC §5.2-§5.4, §11 S1).

The real service over the real ephemeral store on the evaluating fake (`tests/support/voice_mcp_kit.py`); assertions
are on the minted claims, the stored documents and what verification returns.
"""
import logging
import time

import jwt
import pytest

from src.domain.mcp_resource import MCPResource
from src.domain.secret_digest import fingerprint, keyed_digest
from src.domain.voice_mcp_token import (
    VOICE_MCP_KID,
    VOICE_MCP_TOKEN_TTL_S,
    VOICE_MCP_TOKEN_TYPE,
    voice_call_key,
)
from src.services.voice_mcp_token_service import VoiceMcpTokenService, VoiceMcpTokens, is_voice_token
from tests.support.mcp_gate_fakes import FakeUserRepo, REMOVED_EMAIL
from tests.support.voice_mcp_kit import (
    ACCOUNT,
    DIGEST_KEY,
    DRIVE_URI,
    FACTS_URI,
    OAUTH_SECRET,
    OTHER_USER,
    TICKET,
    URIS,
    USER,
    VOICE_KEY,
    Kit,
    claims,
    resign,
)

CID = keyed_digest(DIGEST_KEY, "voice_mcp", TICKET)


@pytest.fixture
def kit() -> Kit:
    return Kit()


class TestMint:
    async def test_one_drive_token_with_the_table_claims(self, kit):
        # Memory reaches a call by delegation, not by MCP (RFC §2 amendment 2026-11-10): the drive token is the only
        # one.
        before = int(time.time())
        tokens = await kit.voice.mint(TICKET, USER, ACCOUNT)
        after = int(time.time())

        drive = claims(tokens.drive)
        assert set(tokens.to_wire()) == {"drive"}
        assert jwt.get_unverified_header(tokens.drive) == {"alg": "HS256", "typ": "JWT", "kid": VOICE_MCP_KID}
        assert {k: v for k, v in drive.items() if k not in ("iat", "exp")} == {
            "sub": USER, "account_id": ACCOUNT, "type": VOICE_MCP_TOKEN_TYPE, "aud": f"voice:{DRIVE_URI}",
            "scope": "drive_read", "tools": ["search_drive", "find_passages", "read_passages"], "cid": CID}
        assert before <= drive["iat"] <= after and drive["exp"] == drive["iat"] + 3600
        assert VOICE_MCP_TOKEN_TTL_S == 3600

    async def test_signed_with_the_voice_key_and_not_the_oauth_secret(self, kit):
        tokens = await kit.voice.mint(TICKET, USER, ACCOUNT)
        jwt.decode(tokens.drive, VOICE_KEY, algorithms=["HS256"], audience=f"voice:{DRIVE_URI}")
        with pytest.raises(jwt.InvalidSignatureError):
            jwt.decode(tokens.drive, OAUTH_SECRET, algorithms=["HS256"], audience=f"voice:{DRIVE_URI}")

    async def test_the_cid_is_the_keyed_digest_of_the_ticket_never_the_ticket(self, kit):
        tokens = await kit.voice.mint(TICKET, USER, ACCOUNT)
        assert claims(tokens.drive)["cid"] == CID == keyed_digest(DIGEST_KEY, "voice_mcp", TICKET)
        assert TICKET not in tokens.drive and TICKET not in str(kit.docs)

    async def test_the_live_call_record_holds_the_user_and_account_only_for_the_token_ttl(self, kit):
        await kit.voice.mint(TICKET, USER, ACCOUNT)
        doc = kit.docs[voice_call_key(CID)]
        assert doc["value"] == {"user_id": USER, "account_id": ACCOUNT}
        remaining = (doc["expires_at"].timestamp() - time.time())
        assert 3590 < remaining <= 3600

    async def test_no_store_holds_a_token(self, kit):
        tokens = await kit.voice.mint(TICKET, USER, ACCOUNT)
        stored = str(kit.docs)
        assert tokens.drive not in stored

    async def test_the_tokens_repr_shows_no_token(self, kit):
        tokens = await kit.voice.mint(TICKET, USER, ACCOUNT)
        assert tokens.drive not in repr(tokens)
        assert tokens.to_wire() == {"drive": tokens.drive}


class TestWhitelistAtMint:
    @pytest.mark.parametrize("users, reason", [
        pytest.param({}, "no_user", id="no-user"),
        pytest.param({USER: None}, "no_email", id="no-email"),
        pytest.param({USER: REMOVED_EMAIL}, "not_whitelisted", id="removed"),
    ])
    async def test_a_refused_user_gets_no_tokens_no_record_and_an_alert(self, kit, caplog, users, reason):
        kit.voice._user_repo = FakeUserRepo(users)
        caplog.set_level(logging.INFO)

        assert await kit.voice.mint(TICKET, USER, ACCOUNT) is None

        assert kit.docs == {}
        assert kit.alerts.posted == [f"Voice MCP: no memory or drive tools for call {fingerprint(CID)} "
                                     f"(user {USER[:8]}): {reason}"]
        assert any(f"reason={reason}" in r.getMessage() and "client=voice" in r.getMessage() for r in caplog.records)

    async def test_a_failing_alert_still_returns_no_tokens(self, kit):
        kit.voice._user_repo = FakeUserRepo({})

        async def broken(_text):
            raise RuntimeError("webhook down")
        kit.alerts.post = broken
        assert await kit.voice.mint(TICKET, USER, ACCOUNT) is None

    async def test_without_an_alert_sink_the_refusal_is_logged_only(self, kit):
        service = VoiceMcpTokenService(
            signing_key=VOICE_KEY, store=kit.store, digest_key=DIGEST_KEY, resource_uris=URIS,
            scope_of={MCPResource.FACTS: "user_context", MCPResource.DRIVE: "drive_read"},
            user_repo=FakeUserRepo({}), whitelist_repo=kit.whitelist_repo, alert_sink=None)
        assert await service.mint(TICKET, USER, ACCOUNT) is None

    async def test_a_whitelist_read_error_propagates_and_mints_nothing(self, kit):
        kit.whitelist_repo.error = RuntimeError("firestore down")
        with pytest.raises(RuntimeError):
            await kit.voice.mint(TICKET, USER, ACCOUNT)
        assert kit.docs == {}


class TestVerify:
    async def test_a_minted_token_verifies_on_its_own_resource_with_its_claims(self, kit):
        tokens = await kit.voice.mint(TICKET, USER, ACCOUNT)

        drive = await kit.voice.verify(tokens.drive, MCPResource.DRIVE)

        assert (drive.user_id, drive.account_id, drive.scopes, drive.tools, drive.cid) == (
            USER, ACCOUNT, ["drive_read"], ["search_drive", "find_passages", "read_passages"], CID)
        assert drive.resource == f"voice:{DRIVE_URI}" and drive.expires_at == claims(tokens.drive)["exp"]
        assert tokens.drive not in repr(drive)

    async def test_the_facts_resource_refuses_every_voice_token(self, kit):
        # The facts resource has no voice token since the RFC §2 amendment (2026-11-10): the drive token is refused
        # there, and so is a voice-signed token re-aimed at the facts audience with the facts tool claimed.
        tokens = await kit.voice.mint(TICKET, USER, ACCOUNT)
        aimed = resign(tokens.drive, VOICE_KEY, aud=f"voice:{FACTS_URI}", scope="user_context",
                       tools=["get_user_context"])
        assert await kit.voice.verify(tokens.drive, MCPResource.FACTS) is None
        assert await kit.voice.verify(aimed, MCPResource.FACTS) is None

    async def test_an_expired_token_is_refused(self):
        kit = Kit(clock=lambda: time.time() - 7200)
        tokens = await kit.voice.mint(TICKET, USER, ACCOUNT)
        assert await kit.voice.verify(tokens.drive, MCPResource.DRIVE) is None

    @pytest.mark.parametrize("changes", [
        pytest.param({"type": "mcp_access"}, id="oauth-type"),
        pytest.param({"type": None}, id="no-type"),
        pytest.param({"aud": DRIVE_URI}, id="oauth-audience"),
        pytest.param({"aud": f"voice:{FACTS_URI}"}, id="facts-audience"),
        pytest.param({"aud": None}, id="no-audience"),
        pytest.param({"aud": [f"voice:{DRIVE_URI}", DRIVE_URI]}, id="list-audience"),
        pytest.param({"exp": None}, id="no-exp"),
        pytest.param({"tools": "search_drive"}, id="tools-not-a-list"),
        pytest.param({"tools": None}, id="no-tools"),
        pytest.param({"cid": None}, id="no-cid"),
        pytest.param({"account_id": ""}, id="empty-account"),
        pytest.param({"iat": int(time.time()) + 3000}, id="issued-in-the-future"),
    ])
    async def test_a_voice_signed_token_with_a_wrong_claim_is_refused(self, kit, changes):
        tokens = await kit.voice.mint(TICKET, USER, ACCOUNT)
        assert await kit.voice.verify(resign(tokens.drive, VOICE_KEY, **changes), MCPResource.DRIVE) is None

    async def test_a_token_signed_with_another_key_is_refused(self, kit):
        tokens = await kit.voice.mint(TICKET, USER, ACCOUNT)
        assert await kit.voice.verify(resign(tokens.drive, OAUTH_SECRET), MCPResource.DRIVE) is None
        assert await kit.voice.verify(resign(tokens.drive, "x" * 40), MCPResource.DRIVE) is None

    async def test_a_voice_signed_token_without_the_voice_kid_is_refused(self, kit):
        tokens = await kit.voice.mint(TICKET, USER, ACCOUNT)
        assert await kit.voice.verify(resign(tokens.drive, VOICE_KEY, header={}), MCPResource.DRIVE) is None
        assert await kit.voice.verify(resign(tokens.drive, VOICE_KEY, header={"kid": "other"}),
                                      MCPResource.DRIVE) is None

    async def test_an_oauth_token_with_a_forged_voice_kid_is_refused(self, kit):
        # On the drive: the facts resource now refuses every voice token before the key check.
        forged = resign(kit.oauth_token(MCPResource.DRIVE), OAUTH_SECRET, header={"kid": VOICE_MCP_KID})
        assert is_voice_token(forged)
        assert await kit.voice.verify(forged, MCPResource.DRIVE) is None
        assert kit.store.reads == []        # refused on the key, before any record read

    async def test_garbage_is_refused(self, kit):
        assert await kit.voice.verify("not-a-jwt", MCPResource.DRIVE) is None

    async def test_no_live_call_record_refuses(self, kit):
        tokens = await kit.voice.mint(TICKET, USER, ACCOUNT)
        await kit.store.inner.delete(voice_call_key(CID))
        assert await kit.voice.verify(tokens.drive, MCPResource.DRIVE) is None

    async def test_a_record_of_another_user_refuses(self, kit):
        tokens = await kit.voice.mint(TICKET, USER, ACCOUNT)
        await kit.store.inner.set(voice_call_key(CID), {"user_id": OTHER_USER}, ttl_s=60)
        assert await kit.voice.verify(tokens.drive, MCPResource.DRIVE) is None

    async def test_a_token_for_another_user_on_this_call_record_refuses(self, kit):
        tokens = await kit.voice.mint(TICKET, USER, ACCOUNT)
        forged = resign(tokens.drive, VOICE_KEY, sub=OTHER_USER)
        assert await kit.voice.verify(forged, MCPResource.DRIVE) is None

    async def test_a_store_error_refuses(self, kit, caplog):
        tokens = await kit.voice.mint(TICKET, USER, ACCOUNT)

        async def broken(_key):
            raise RuntimeError("firestore down")
        kit.store.inner.get = broken
        caplog.set_level(logging.INFO)
        assert await kit.voice.verify(tokens.drive, MCPResource.DRIVE) is None
        assert any("reason=store_error" in r.getMessage() and "RuntimeError" in r.getMessage() for r in caplog.records)

    async def test_a_refusal_logs_the_reason_only(self, kit, caplog):
        tokens = await kit.voice.mint(TICKET, USER, ACCOUNT)
        caplog.set_level(logging.DEBUG)
        await kit.voice.verify(resign(tokens.drive, VOICE_KEY, aud=f"voice:{FACTS_URI}"), MCPResource.DRIVE)
        lines = [r.getMessage() for r in caplog.records if "[VoiceMCP] token refused" in r.getMessage()]
        assert lines == ["[VoiceMCP] token refused: resource=drive reason=audience client=voice"]


class TestRevoke:
    async def test_after_revoke_the_token_fails(self, kit):
        tokens = await kit.voice.mint(TICKET, USER, ACCOUNT)
        await kit.voice.revoke(TICKET)
        assert kit.docs == {}
        assert await kit.voice.verify(tokens.drive, MCPResource.DRIVE) is None

    async def test_revoke_without_a_record_is_a_no_op(self, kit):
        await kit.voice.revoke(TICKET)
        assert kit.docs == {}

    async def test_revoke_of_another_call_leaves_this_one_alive(self, kit):
        tokens = await kit.voice.mint(TICKET, USER, ACCOUNT)
        await kit.voice.revoke("ticket-0002-invented")
        assert await kit.voice.verify(tokens.drive, MCPResource.DRIVE) is not None


class TestKeyAndRouting:
    def test_a_short_key_refuses_to_build(self, kit):
        with pytest.raises(ValueError, match="at least 48"):
            VoiceMcpTokenService(signing_key="k" * 47, store=kit.store, digest_key=DIGEST_KEY, resource_uris=URIS,
                                 scope_of={}, user_repo=kit.user_repo, whitelist_repo=kit.whitelist_repo,
                                 alert_sink=None)

    async def test_is_voice_token_reads_the_kid_only(self, kit):
        tokens = await kit.voice.mint(TICKET, USER, ACCOUNT)
        assert is_voice_token(tokens.drive)
        assert not is_voice_token(kit.oauth_token(MCPResource.FACTS))
        assert not is_voice_token(kit.oauth_token(MCPResource.DRIVE))
        assert not is_voice_token("not-a-jwt")
        assert not is_voice_token(resign(tokens.drive, VOICE_KEY, header={"kid": "voice-mcp-2"}))


async def test_no_log_record_contains_a_token_or_the_ticket(kit, caplog):
    caplog.set_level(logging.DEBUG)
    tokens = await kit.voice.mint(TICKET, USER, ACCOUNT)
    await kit.voice.verify(tokens.drive, MCPResource.DRIVE)
    await kit.voice.verify(tokens.drive, MCPResource.FACTS)          # a refusal
    await kit.voice.verify(resign(tokens.drive, OAUTH_SECRET), MCPResource.DRIVE)
    await kit.voice.revoke(TICKET)
    await kit.voice.verify(tokens.drive, MCPResource.DRIVE)          # a refusal after revoke
    text = "\n".join(r.getMessage() + str(r.args) for r in caplog.records)
    assert text and "client=voice" in text
    for secret in (tokens.drive, TICKET, VOICE_KEY):
        assert secret not in text
    for part in tokens.drive.split("."):
        assert part not in text


def test_the_tokens_dataclass_hides_its_value():
    tokens = VoiceMcpTokens(drive="secret-drive-token")
    assert "secret" not in repr(tokens)
