"""
Unit tests for ShortLinkService — mints short `/s/<code>` aliases for long
capability links, backed by ShortLinkRepositoryPort.
"""
import string
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from src.domain.short_link import ShortLink
from src.ports.short_link_repository_port import ShortLinkRepositoryPort
from src.services.short_link_service import ShortLinkService

_TARGET = "https://dev.alekbot.app/f/some-long-token"


@pytest.fixture
def repo():
    r = AsyncMock(spec=ShortLinkRepositoryPort)
    r.create_if_absent.return_value = True
    return r


@pytest.fixture
def svc(repo):
    return ShortLinkService(repository=repo, base_url="https://dev.alekbot.app/")


class TestShorten:

    async def test_returns_url_with_s_prefix(self, svc):
        url = await svc.shorten(_TARGET, ttl_seconds=3600)
        assert url.startswith("https://dev.alekbot.app/s/")
        # base_url trailing slash must be normalized (no double slash before /s/)
        assert "//s/" not in url.replace("https://", "")

    async def test_code_is_base62_of_expected_length(self, svc):
        url = await svc.shorten(_TARGET, ttl_seconds=3600)
        code = url.rsplit("/s/", 1)[1]
        assert len(code) == 10
        assert all(c in string.ascii_letters + string.digits for c in code)

    async def test_stores_target_url_via_repository(self, svc, repo):
        await svc.shorten(_TARGET, ttl_seconds=3600)
        stored: ShortLink = repo.create_if_absent.call_args.args[0]
        assert stored.target_url == _TARGET

    async def test_stores_expiry_ttl_seconds_from_now(self, svc, repo):
        before = datetime.now(timezone.utc)
        await svc.shorten(_TARGET, ttl_seconds=3600)
        stored: ShortLink = repo.create_if_absent.call_args.args[0]
        assert stored.expires_at - before - timedelta(seconds=3600) < timedelta(seconds=5)

    async def test_retries_on_code_collision(self, svc, repo):
        repo.create_if_absent.side_effect = [False, True]
        url = await svc.shorten(_TARGET, ttl_seconds=3600)
        assert repo.create_if_absent.await_count == 2
        first_code = repo.create_if_absent.call_args_list[0].args[0].code
        second_code = repo.create_if_absent.call_args_list[1].args[0].code
        assert first_code != second_code
        assert url.endswith(second_code)

    async def test_raises_after_exhausting_retry_attempts(self, svc, repo):
        repo.create_if_absent.return_value = False
        with pytest.raises(RuntimeError):
            await svc.shorten(_TARGET, ttl_seconds=3600)


class TestResolve:

    async def test_returns_target_url_when_found(self, svc, repo):
        repo.resolve.return_value = ShortLink(
            code="abc1234567", target_url=_TARGET,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        result = await svc.resolve("abc1234567")
        assert result == _TARGET
        repo.resolve.assert_awaited_once_with("abc1234567")

    async def test_returns_none_when_not_found(self, svc, repo):
        repo.resolve.return_value = None
        result = await svc.resolve("missing123")
        assert result is None
