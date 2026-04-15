"""
Port contract tests for MCPClientRepository.

- Is an ABC (abstract, not instantiable)
- Declares exactly the methods the service calls
- A minimal in-memory fake round-trips all three entity types
"""

import inspect
from abc import ABC
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from src.domain.mcp import MCPAuthCode, MCPClient, MCPRefreshToken
from src.ports.mcp_client_repository import MCPClientRepository


def _abstract_methods(cls) -> set:
    return {
        name
        for name, method in inspect.getmembers(cls)
        if getattr(method, "__isabstractmethod__", False)
    }


# ---------------------------------------------------------------------------
# ABC contract
# ---------------------------------------------------------------------------


class TestPortContract:
    def test_is_abstract(self):
        assert issubclass(MCPClientRepository, ABC)

    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            MCPClientRepository()

    def test_abstract_method_set(self):
        assert _abstract_methods(MCPClientRepository) == {
            "save_client",
            "get_client",
            "delete_client",
            "save_auth_code",
            "get_auth_code",
            "consume_auth_code",
            "save_refresh_token",
            "get_refresh_token",
            "revoke_refresh_token",
        }


# ---------------------------------------------------------------------------
# Reference in-memory fake
# ---------------------------------------------------------------------------


class InMemoryMCPRepo(MCPClientRepository):
    def __init__(self):
        self.clients: dict[str, MCPClient] = {}
        self.codes: dict[str, MCPAuthCode] = {}
        self.refresh_tokens: dict[str, MCPRefreshToken] = {}

    async def save_client(self, client: MCPClient) -> None:
        self.clients[client.client_id] = client

    async def get_client(self, client_id: str) -> Optional[MCPClient]:
        return self.clients.get(client_id)

    async def delete_client(self, client_id: str) -> None:
        self.clients.pop(client_id, None)

    async def save_auth_code(self, auth_code: MCPAuthCode) -> None:
        self.codes[auth_code.code] = auth_code

    async def get_auth_code(self, code: str) -> Optional[MCPAuthCode]:
        return self.codes.get(code)

    async def consume_auth_code(self, code: str) -> Optional[MCPAuthCode]:
        return self.codes.pop(code, None)

    async def save_refresh_token(self, token: MCPRefreshToken) -> None:
        self.refresh_tokens[token.token_hash] = token

    async def get_refresh_token(self, token_hash: str) -> Optional[MCPRefreshToken]:
        return self.refresh_tokens.get(token_hash)

    async def revoke_refresh_token(self, token_hash: str) -> None:
        existing = self.refresh_tokens.get(token_hash)
        if existing:
            self.refresh_tokens[token_hash] = existing.model_copy(
                update={"revoked_at": datetime.now(timezone.utc)}
            )


@pytest.fixture
def repo():
    return InMemoryMCPRepo()


@pytest.fixture
def client_obj():
    return MCPClient(
        client_id="mcp-test-1",
        client_secret="s",
        client_name="Claude",
        redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def auth_code_obj():
    return MCPAuthCode(
        code="code-1",
        client_id="mcp-test-1",
        user_id="u",
        account_id="a",
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
        code_challenge="x" * 43,
        scopes=["user_context"],
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )


@pytest.fixture
def refresh_token_obj():
    return MCPRefreshToken(
        token_hash="abc123",
        client_id="mcp-test-1",
        user_id="u",
        account_id="a",
        scopes=["user_context"],
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )


# ---------------------------------------------------------------------------
# Roundtrip tests against the reference fake
# ---------------------------------------------------------------------------


class TestClientRoundtrip:
    async def test_save_then_get(self, repo, client_obj):
        await repo.save_client(client_obj)
        got = await repo.get_client("mcp-test-1")
        assert got is not None
        assert got.client_secret == "s"

    async def test_get_missing_returns_none(self, repo):
        assert await repo.get_client("nope") is None

    async def test_delete_removes(self, repo, client_obj):
        await repo.save_client(client_obj)
        await repo.delete_client("mcp-test-1")
        assert await repo.get_client("mcp-test-1") is None


class TestAuthCodeRoundtrip:
    async def test_get_is_non_consuming(self, repo, auth_code_obj):
        await repo.save_auth_code(auth_code_obj)
        first = await repo.get_auth_code("code-1")
        second = await repo.get_auth_code("code-1")
        assert first is not None and second is not None

    async def test_consume_is_one_shot(self, repo, auth_code_obj):
        await repo.save_auth_code(auth_code_obj)
        first = await repo.consume_auth_code("code-1")
        second = await repo.consume_auth_code("code-1")
        assert first is not None
        assert second is None


class TestRefreshTokenRoundtrip:
    async def test_save_get(self, repo, refresh_token_obj):
        await repo.save_refresh_token(refresh_token_obj)
        got = await repo.get_refresh_token("abc123")
        assert got is not None
        assert got.is_active

    async def test_revoke_sets_revoked_at(self, repo, refresh_token_obj):
        await repo.save_refresh_token(refresh_token_obj)
        await repo.revoke_refresh_token("abc123")
        got = await repo.get_refresh_token("abc123")
        assert got is not None
        assert not got.is_active
        assert got.revoked_at is not None

    async def test_revoke_missing_is_noop(self, repo):
        await repo.revoke_refresh_token("does-not-exist")  # should not raise
