"""
Unit tests for core port contracts.

Verifies that each ABC port interface:
1. Cannot be instantiated directly.
2. Declares all expected abstract methods.
3. Has no unexpected additions (method count assertion).
4. Exposes correct signatures for key methods.

Ports covered (19):
  AccountRepository, AudioTranscriptionPort, AuthPort,
  ConversationHandlerPort, EmbeddingService, FactManagementPort,
  FactWritePort, FactRepository, FileService, IAMPort,
  InviteCodeRepository, LLMService, LogSink (Protocol),
  PlatformAuthPort, QuotaService, SearchEnrichmentPort,
  TaskQueue (Protocol), UserRepository, WhitelistRepository.
"""

import inspect
import pytest
from abc import ABC
from typing import get_type_hints
from unittest.mock import AsyncMock, MagicMock

from src.ports.account_repository import AccountRepository
from src.ports.audio_transcription_port import AudioTranscriptionPort
from src.ports.auth_port import AuthPort
from src.ports.conversation_handler_port import ConversationHandlerPort
from src.ports.embedding_service import EmbeddingService
from src.ports.fact_management_port import FactManagementPort
from src.ports.fact_write_port import FactWritePort
from src.ports.file_service import FileService
from src.ports.iam_port import IAMPort, ResourceType, Action, Role
from src.ports.invite_code_repository import InviteCodeRepository
from src.ports.llm_service import LLMService, LLMResponse
from src.ports.log_sink import LogSink
from src.ports.platform_auth_port import PlatformAuthPort, IAMDecision
from src.ports.quota_service import QuotaService
from src.ports.repository import FactRepository
from src.ports.search_enrichment_port import SearchEnrichmentPort
from src.ports.task_queue import TaskQueue
from src.ports.user_repository import UserRepository
from src.ports.whitelist_repository import WhitelistRepository


# =============================================================================
# AccountRepository
# =============================================================================

class TestAccountRepositoryContract:
    def test_is_abstract_class(self):
        assert issubclass(AccountRepository, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            AccountRepository()

    def test_has_get_account(self):
        assert getattr(AccountRepository.get_account, "__isabstractmethod__", False)

    def test_has_create_account(self):
        assert getattr(AccountRepository.create_account, "__isabstractmethod__", False)

    def test_has_update_account(self):
        assert getattr(AccountRepository.update_account, "__isabstractmethod__", False)

    def test_has_increment_account_usage(self):
        assert getattr(AccountRepository.increment_account_usage, "__isabstractmethod__", False)

    def test_has_check_quota(self):
        assert getattr(AccountRepository.check_quota, "__isabstractmethod__", False)

    def test_all_abstract_methods_count(self):
        abstract_methods = {
            name for name, method in inspect.getmembers(AccountRepository)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 5, f"Expected 5 abstract methods, got {abstract_methods}"


class TestAccountRepositoryMockImplementation:
    @pytest.fixture
    def mock_repo(self):
        return AsyncMock(spec=AccountRepository)

    async def test_get_account(self, mock_repo):
        mock_repo.get_account.return_value = None
        result = await mock_repo.get_account("acc1")
        assert result is None

    async def test_create_account(self, mock_repo):
        account = MagicMock()
        mock_repo.create_account.return_value = account
        result = await mock_repo.create_account(account)
        assert result is account

    async def test_check_quota(self, mock_repo):
        mock_repo.check_quota.return_value = (True, "within quota")
        result = await mock_repo.check_quota("acc1")
        assert result == (True, "within quota")

    async def test_increment_account_usage(self, mock_repo):
        await mock_repo.increment_account_usage("acc1", 500, 0.05)
        mock_repo.increment_account_usage.assert_called_once_with("acc1", 500, 0.05)


# =============================================================================
# AudioTranscriptionPort
# =============================================================================

class TestAudioTranscriptionPortContract:
    def test_is_abstract_class(self):
        assert issubclass(AudioTranscriptionPort, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            AudioTranscriptionPort()

    def test_has_transcribe(self):
        assert getattr(AudioTranscriptionPort.transcribe, "__isabstractmethod__", False)

    def test_all_abstract_methods_count(self):
        abstract_methods = {
            name for name, method in inspect.getmembers(AudioTranscriptionPort)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 1, f"Expected 1 abstract method, got {abstract_methods}"

    def test_transcribe_signature(self):
        sig = inspect.signature(AudioTranscriptionPort.transcribe)
        params = list(sig.parameters.keys())
        assert params == ["self", "local_path", "mime_type"]
        assert sig.return_annotation == str


class TestAudioTranscriptionPortMockImplementation:
    @pytest.fixture
    def mock_port(self):
        return AsyncMock(spec=AudioTranscriptionPort)

    async def test_transcribe_returns_string(self, mock_port):
        mock_port.transcribe.return_value = "transcribed text"
        result = await mock_port.transcribe("/tmp/audio.mp3", "audio/mpeg")
        assert isinstance(result, str)
        assert result == "transcribed text"


# =============================================================================
# AuthPort
# =============================================================================

class TestAuthPortContract:
    def test_is_abstract_class(self):
        assert issubclass(AuthPort, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            AuthPort()

    def test_has_get_provider_name(self):
        assert getattr(AuthPort.get_provider_name, "__isabstractmethod__", False)

    def test_has_get_authorization_url(self):
        assert getattr(AuthPort.get_authorization_url, "__isabstractmethod__", False)

    def test_has_exchange_code_for_tokens(self):
        assert getattr(AuthPort.exchange_code_for_tokens, "__isabstractmethod__", False)

    def test_has_verify_token(self):
        assert getattr(AuthPort.verify_token, "__isabstractmethod__", False)

    def test_has_get_user_info(self):
        assert getattr(AuthPort.get_user_info, "__isabstractmethod__", False)

    def test_has_refresh_access_token(self):
        assert getattr(AuthPort.refresh_access_token, "__isabstractmethod__", False)

    def test_all_abstract_methods_count(self):
        abstract_methods = {
            name for name, method in inspect.getmembers(AuthPort)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 6, f"Expected 6 abstract methods, got {abstract_methods}"

    def test_get_authorization_url_signature(self):
        sig = inspect.signature(AuthPort.get_authorization_url)
        params = list(sig.parameters.keys())
        assert params == ["self", "state", "redirect_uri"]
        assert sig.return_annotation == str

    def test_get_provider_name_is_sync(self):
        """get_provider_name must be sync (no async)."""
        assert not inspect.iscoroutinefunction(AuthPort.get_provider_name)

    def test_get_authorization_url_is_sync(self):
        assert not inspect.iscoroutinefunction(AuthPort.get_authorization_url)

    def test_exchange_code_for_tokens_is_async(self):
        assert inspect.iscoroutinefunction(AuthPort.exchange_code_for_tokens)


class TestAuthPortMockImplementation:
    @pytest.fixture
    def mock_port(self):
        return AsyncMock(spec=AuthPort)

    def test_get_provider_name(self, mock_port):
        mock_port.get_provider_name.return_value = "firebase"
        result = mock_port.get_provider_name()
        assert result == "firebase"

    def test_get_authorization_url(self, mock_port):
        mock_port.get_authorization_url.return_value = "https://auth.example.com/oauth"
        result = mock_port.get_authorization_url("state123", "https://app.com/callback")
        assert isinstance(result, str)

    async def test_exchange_code_for_tokens(self, mock_port):
        tokens = MagicMock()
        mock_port.exchange_code_for_tokens.return_value = tokens
        result = await mock_port.exchange_code_for_tokens("code123", "https://app.com/callback")
        assert result is tokens

    async def test_verify_token(self, mock_port):
        claims = MagicMock()
        mock_port.verify_token.return_value = claims
        result = await mock_port.verify_token("id_token_jwt")
        assert result is claims


# =============================================================================
# ConversationHandlerPort
# =============================================================================

class TestConversationHandlerPortContract:
    def test_is_abstract_class(self):
        assert issubclass(ConversationHandlerPort, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            ConversationHandlerPort()

    def test_has_handle_message(self):
        assert getattr(ConversationHandlerPort.handle_message, "__isabstractmethod__", False)

    def test_has_handle_command(self):
        assert getattr(ConversationHandlerPort.handle_command, "__isabstractmethod__", False)

    def test_all_abstract_methods_count(self):
        abstract_methods = {
            name for name, method in inspect.getmembers(ConversationHandlerPort)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 2, f"Expected 2 abstract methods, got {abstract_methods}"

    def test_handle_message_signature(self):
        sig = inspect.signature(ConversationHandlerPort.handle_message)
        params = list(sig.parameters.keys())
        assert params == ["self", "context", "response_channel"]

    def test_handle_command_signature(self):
        sig = inspect.signature(ConversationHandlerPort.handle_command)
        params = list(sig.parameters.keys())
        assert params == ["self", "command", "context", "response_channel"]


class TestConversationHandlerPortMockImplementation:
    @pytest.fixture
    def mock_port(self):
        return AsyncMock(spec=ConversationHandlerPort)

    async def test_handle_message(self, mock_port):
        ctx, channel = MagicMock(), MagicMock()
        await mock_port.handle_message(ctx, channel)
        mock_port.handle_message.assert_called_once_with(ctx, channel)

    async def test_handle_command(self, mock_port):
        ctx, channel = MagicMock(), MagicMock()
        await mock_port.handle_command("/status", ctx, channel)
        mock_port.handle_command.assert_called_once_with("/status", ctx, channel)


# =============================================================================
# EmbeddingService
# =============================================================================

class TestEmbeddingServiceContract:
    def test_is_abstract_class(self):
        assert issubclass(EmbeddingService, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            EmbeddingService()

    def test_has_get_embedding(self):
        assert getattr(EmbeddingService.get_embedding, "__isabstractmethod__", False)

    def test_all_abstract_methods_count(self):
        abstract_methods = {
            name for name, method in inspect.getmembers(EmbeddingService)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 1, f"Expected 1 abstract method, got {abstract_methods}"

    def test_get_embedding_signature(self):
        sig = inspect.signature(EmbeddingService.get_embedding)
        params = list(sig.parameters.keys())
        assert params == ["self", "text", "task_type"]
        assert sig.parameters["task_type"].default == "RETRIEVAL_DOCUMENT"


class TestEmbeddingServiceMockImplementation:
    @pytest.fixture
    def mock_service(self):
        return AsyncMock(spec=EmbeddingService)

    async def test_get_embedding_returns_list(self, mock_service):
        mock_service.get_embedding.return_value = [0.1, 0.2, 0.3]
        result = await mock_service.get_embedding("some text")
        assert isinstance(result, list)
        assert len(result) == 3


# =============================================================================
# FactManagementPort
# =============================================================================

class TestFactManagementPortContract:
    def test_is_abstract_class(self):
        assert issubclass(FactManagementPort, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            FactManagementPort()

    def test_has_search_existing_facts(self):
        assert getattr(FactManagementPort.search_existing_facts, "__isabstractmethod__", False)

    def test_has_merge_facts(self):
        assert getattr(FactManagementPort.merge_facts, "__isabstractmethod__", False)

    def test_has_discard_candidate(self):
        assert getattr(FactManagementPort.discard_candidate, "__isabstractmethod__", False)

    def test_has_create_fact(self):
        assert getattr(FactManagementPort.create_fact, "__isabstractmethod__", False)

    def test_has_update_fact(self):
        assert getattr(FactManagementPort.update_fact, "__isabstractmethod__", False)

    def test_all_abstract_methods_count(self):
        abstract_methods = {
            name for name, method in inspect.getmembers(FactManagementPort)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 5, f"Expected 5 abstract methods, got {abstract_methods}"

    def test_search_existing_facts_signature(self):
        sig = inspect.signature(FactManagementPort.search_existing_facts)
        params = list(sig.parameters.keys())
        assert params == ["self", "keywords", "primary_query", "alternative_query", "limit"]
        assert sig.parameters["alternative_query"].default == ""
        assert sig.parameters["limit"].default == 20


class TestFactManagementPortMockImplementation:
    @pytest.fixture
    def mock_port(self):
        return AsyncMock(spec=FactManagementPort)

    async def test_search_existing_facts(self, mock_port):
        mock_port.search_existing_facts.return_value = []
        result = await mock_port.search_existing_facts(["kw1"], "main query")
        assert isinstance(result, list)

    async def test_merge_facts(self, mock_port):
        mock_port.merge_facts.return_value = {"fact_id": "new", "content": "merged"}
        result = await mock_port.merge_facts(["f1", "f2"], "merged content", {})
        assert isinstance(result, dict)

    async def test_discard_candidate(self, mock_port):
        mock_port.discard_candidate.return_value = {"status": "discarded"}
        result = await mock_port.discard_candidate("duplicate")
        assert isinstance(result, dict)

    async def test_create_fact(self, mock_port):
        mock_port.create_fact.return_value = {"fact_id": "abc123", "status": "created", "message": "ok"}
        result = await mock_port.create_fact("User owns a cat", {"account_id": "a1", "user_id": "u1"})
        assert isinstance(result, dict)
        assert "fact_id" in result

    async def test_update_fact(self, mock_port):
        mock_port.update_fact.return_value = {"fact_id": "abc123", "status": "updated", "message": "ok"}
        result = await mock_port.update_fact("abc123", {"content": "User owns two cats"})
        assert isinstance(result, dict)
        assert "status" in result

    def test_create_fact_signature(self):
        sig = inspect.signature(FactManagementPort.create_fact)
        params = list(sig.parameters.keys())
        assert params == ["self", "content", "metadata"]

    def test_update_fact_signature(self):
        sig = inspect.signature(FactManagementPort.update_fact)
        params = list(sig.parameters.keys())
        assert params == ["self", "fact_id", "updates"]


# =============================================================================
# FactWritePort
# =============================================================================

class TestFactWritePortContract:
    def test_is_abstract_class(self):
        assert issubclass(FactWritePort, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            FactWritePort()

    def test_has_add_facts_batch(self):
        assert getattr(FactWritePort.add_facts_batch, "__isabstractmethod__", False)

    def test_all_abstract_methods_count(self):
        abstract_methods = {
            name for name, method in inspect.getmembers(FactWritePort)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 1, f"Expected 1 abstract method, got {abstract_methods}"

    def test_add_facts_batch_signature(self):
        sig = inspect.signature(FactWritePort.add_facts_batch)
        params = list(sig.parameters.keys())
        assert params == ["self", "account_id", "user_id", "facts_data", "skip_deduplication"]
        assert sig.parameters["skip_deduplication"].default is False


class TestFactWritePortMockImplementation:
    @pytest.fixture
    def mock_port(self):
        return AsyncMock(spec=FactWritePort)

    async def test_add_facts_batch_returns_tuple(self, mock_port):
        mock_port.add_facts_batch.return_value = (3, 1)
        result = await mock_port.add_facts_batch("acc1", "u1", [{"content": "fact"}])
        assert result == (3, 1)


# =============================================================================
# FactRepository
# =============================================================================

class TestFactRepositoryContract:
    def test_is_abstract_class(self):
        assert issubclass(FactRepository, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            FactRepository()

    def test_has_add_fact(self):
        assert getattr(FactRepository.add_fact, "__isabstractmethod__", False)

    def test_has_get_fact_by_id(self):
        assert getattr(FactRepository.get_fact_by_id, "__isabstractmethod__", False)

    def test_has_get_facts_by_ids(self):
        assert getattr(FactRepository.get_facts_by_ids, "__isabstractmethod__", False)

    def test_has_get_active_facts(self):
        assert getattr(FactRepository.get_active_facts, "__isabstractmethod__", False)

    def test_has_get_paginated_facts(self):
        assert getattr(FactRepository.get_paginated_facts, "__isabstractmethod__", False)

    def test_has_search_facts(self):
        assert getattr(FactRepository.search_facts, "__isabstractmethod__", False)

    def test_has_update_fact(self):
        assert getattr(FactRepository.update_fact, "__isabstractmethod__", False)

    def test_has_get_lineage(self):
        assert getattr(FactRepository.get_lineage, "__isabstractmethod__", False)

    def test_has_get_latest_fact_by_lineage(self):
        assert getattr(FactRepository.get_latest_fact_by_lineage, "__isabstractmethod__", False)

    def test_has_add_observation(self):
        assert getattr(FactRepository.add_observation, "__isabstractmethod__", False)

    def test_has_get_observations(self):
        assert getattr(FactRepository.get_observations, "__isabstractmethod__", False)

    def test_has_archive_observations(self):
        assert getattr(FactRepository.archive_observations, "__isabstractmethod__", False)

    def test_has_add_fact_if_unique(self):
        assert getattr(FactRepository.add_fact_if_unique, "__isabstractmethod__", False)

    def test_has_get_biographical_context(self):
        assert getattr(FactRepository.get_biographical_context, "__isabstractmethod__", False)

    def test_has_refresh_biographical_context_cache(self):
        assert getattr(FactRepository.refresh_biographical_context_cache, "__isabstractmethod__", False)

    def test_has_get_biographical_context_cached(self):
        assert getattr(FactRepository.get_biographical_context_cached, "__isabstractmethod__", False)

    def test_has_invalidate_fact(self):
        assert getattr(FactRepository.invalidate_fact, "__isabstractmethod__", False)

    def test_has_get_legacy_facts(self):
        assert getattr(FactRepository.get_legacy_facts, "__isabstractmethod__", False)

    def test_all_abstract_methods_count(self):
        """Port should have exactly 18 abstract methods."""
        abstract_methods = {
            name for name, method in inspect.getmembers(FactRepository)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 18, f"Expected 18 abstract methods, got {abstract_methods}"

    def test_add_fact_signature(self):
        sig = inspect.signature(FactRepository.add_fact)
        params = list(sig.parameters.keys())
        assert params == ["self", "fact"]
        assert sig.return_annotation == str

    def test_search_facts_signature(self):
        sig = inspect.signature(FactRepository.search_facts)
        params = list(sig.parameters.keys())
        assert params == ["self", "query_vector", "limit", "user_id", "account_id"]
        assert sig.parameters["limit"].default == 5
        assert sig.parameters["user_id"].default is None
        assert sig.parameters["account_id"].default is None


class TestFactRepositoryMockImplementation:
    @pytest.fixture
    def mock_repo(self):
        return AsyncMock(spec=FactRepository)

    async def test_add_fact(self, mock_repo):
        mock_repo.add_fact.return_value = "fact-id-123"
        result = await mock_repo.add_fact(MagicMock())
        assert isinstance(result, str)

    async def test_get_fact_by_id_returns_none(self, mock_repo):
        mock_repo.get_fact_by_id.return_value = None
        result = await mock_repo.get_fact_by_id("nonexistent")
        assert result is None

    async def test_search_facts_returns_list(self, mock_repo):
        mock_repo.search_facts.return_value = []
        result = await mock_repo.search_facts([0.1, 0.2], limit=5)
        assert isinstance(result, list)

    async def test_add_fact_if_unique_returns_tuple(self, mock_repo):
        mock_repo.add_fact_if_unique.return_value = (True, None)
        result = await mock_repo.add_fact_if_unique(MagicMock())
        assert result == (True, None)

    async def test_get_paginated_facts_returns_tuple(self, mock_repo):
        mock_repo.get_paginated_facts.return_value = ([], None)
        result = await mock_repo.get_paginated_facts("owner1")
        facts, cursor = result
        assert isinstance(facts, list)
        assert cursor is None


# =============================================================================
# FileService
# =============================================================================

class TestFileServiceContract:
    def test_is_abstract_class(self):
        assert issubclass(FileService, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            FileService()

    def test_has_upload_file(self):
        assert getattr(FileService.upload_file, "__isabstractmethod__", False)

    def test_all_abstract_methods_count(self):
        abstract_methods = {
            name for name, method in inspect.getmembers(FileService)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 1, f"Expected 1 abstract method, got {abstract_methods}"

    def test_upload_file_signature(self):
        sig = inspect.signature(FileService.upload_file)
        params = list(sig.parameters.keys())
        assert params == ["self", "path", "mime_type"]


class TestFileServiceMockImplementation:
    @pytest.fixture
    def mock_service(self):
        return AsyncMock(spec=FileService)

    async def test_upload_file(self, mock_service):
        part = MagicMock()
        mock_service.upload_file.return_value = part
        result = await mock_service.upload_file("/tmp/file.pdf", "application/pdf")
        assert result is part


# =============================================================================
# IAMPort
# =============================================================================

class TestIAMPortContract:
    def test_is_abstract_class(self):
        assert issubclass(IAMPort, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            IAMPort()

    def test_has_can_access_resource(self):
        assert getattr(IAMPort.can_access_resource, "__isabstractmethod__", False)

    def test_has_get_user_role(self):
        assert getattr(IAMPort.get_user_role, "__isabstractmethod__", False)

    def test_has_assign_role(self):
        assert getattr(IAMPort.assign_role, "__isabstractmethod__", False)

    def test_has_revoke_access(self):
        assert getattr(IAMPort.revoke_access, "__isabstractmethod__", False)

    def test_has_get_account_members(self):
        assert getattr(IAMPort.get_account_members, "__isabstractmethod__", False)

    def test_has_permission_is_not_abstract(self):
        """has_permission is a concrete helper method, must NOT be abstract."""
        assert not getattr(IAMPort.has_permission, "__isabstractmethod__", False)

    def test_all_abstract_methods_count(self):
        """Port should have exactly 5 abstract methods (has_permission is concrete)."""
        abstract_methods = {
            name for name, method in inspect.getmembers(IAMPort)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 5, f"Expected 5 abstract methods, got {abstract_methods}"

    def test_has_permission_uses_role_permissions_matrix(self):
        """has_permission is a concrete sync method — verify it works without a DB call."""

        class _ConcreteIAM(IAMPort):
            async def can_access_resource(self, *a, **kw): ...
            async def get_user_role(self, *a, **kw): ...
            async def assign_role(self, *a, **kw): ...
            async def revoke_access(self, *a, **kw): ...
            async def get_account_members(self, *a, **kw): ...

        iam = _ConcreteIAM()
        assert iam.has_permission(Role.OWNER, ResourceType.ACCOUNT, Action.ADMIN) is True
        assert iam.has_permission(Role.VIEWER, ResourceType.ACCOUNT, Action.ADMIN) is False


class TestIAMPortMockImplementation:
    @pytest.fixture
    def mock_port(self):
        return AsyncMock(spec=IAMPort)

    async def test_can_access_resource(self, mock_port):
        mock_port.can_access_resource.return_value = True
        result = await mock_port.can_access_resource(
            "u1", ResourceType.FACT, "fact-1", Action.READ, account_id="acc1"
        )
        assert result is True

    async def test_get_user_role(self, mock_port):
        mock_port.get_user_role.return_value = Role.MEMBER
        result = await mock_port.get_user_role("u1", "acc1")
        assert result == Role.MEMBER

    async def test_get_account_members(self, mock_port):
        mock_port.get_account_members.return_value = {"u1": Role.OWNER}
        result = await mock_port.get_account_members("acc1")
        assert isinstance(result, dict)


# =============================================================================
# InviteCodeRepository
# =============================================================================

class TestInviteCodeRepositoryContract:
    def test_is_abstract_class(self):
        assert issubclass(InviteCodeRepository, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            InviteCodeRepository()

    def test_has_create(self):
        assert getattr(InviteCodeRepository.create, "__isabstractmethod__", False)

    def test_has_get_by_code(self):
        assert getattr(InviteCodeRepository.get_by_code, "__isabstractmethod__", False)

    def test_has_update(self):
        assert getattr(InviteCodeRepository.update, "__isabstractmethod__", False)

    def test_has_list_by_user(self):
        assert getattr(InviteCodeRepository.list_by_user, "__isabstractmethod__", False)

    def test_all_abstract_methods_count(self):
        abstract_methods = {
            name for name, method in inspect.getmembers(InviteCodeRepository)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 4, f"Expected 4 abstract methods, got {abstract_methods}"


class TestInviteCodeRepositoryMockImplementation:
    @pytest.fixture
    def mock_repo(self):
        return AsyncMock(spec=InviteCodeRepository)

    async def test_create(self, mock_repo):
        code = MagicMock()
        mock_repo.create.return_value = code
        result = await mock_repo.create(code)
        assert result is code

    async def test_get_by_code_not_found(self, mock_repo):
        mock_repo.get_by_code.return_value = None
        result = await mock_repo.get_by_code("INVALID")
        assert result is None

    async def test_list_by_user(self, mock_repo):
        mock_repo.list_by_user.return_value = []
        result = await mock_repo.list_by_user("u1")
        assert isinstance(result, list)


# =============================================================================
# LLMService
# =============================================================================

class TestLLMServiceContract:
    def test_is_abstract_class(self):
        assert issubclass(LLMService, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            LLMService()

    def test_has_generate_content(self):
        assert getattr(LLMService.generate_content, "__isabstractmethod__", False)

    def test_has_supports_caching(self):
        assert getattr(LLMService.supports_caching, "__isabstractmethod__", False)

    def test_has_upload_file(self):
        assert getattr(LLMService.upload_file, "__isabstractmethod__", False)

    def test_has_get_capabilities(self):
        assert getattr(LLMService.get_capabilities, "__isabstractmethod__", False)

    def test_has_get_model_for_tier(self):
        assert getattr(LLMService.get_model_for_tier, "__isabstractmethod__", False)

    def test_all_abstract_methods_count(self):
        """Port should have exactly 5 abstract methods."""
        abstract_methods = {
            name for name, method in inspect.getmembers(LLMService)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 5, f"Expected 5 abstract methods, got {abstract_methods}"

    def test_generate_content_is_async(self):
        assert inspect.iscoroutinefunction(LLMService.generate_content)

    def test_supports_caching_is_sync(self):
        assert not inspect.iscoroutinefunction(LLMService.supports_caching)

    def test_get_capabilities_is_sync(self):
        assert not inspect.iscoroutinefunction(LLMService.get_capabilities)

    def test_get_model_for_tier_is_sync(self):
        assert not inspect.iscoroutinefunction(LLMService.get_model_for_tier)

    def test_generate_content_signature(self):
        sig = inspect.signature(LLMService.generate_content)
        params = list(sig.parameters.keys())
        assert "self" in params
        assert "model_name" in params
        assert "system_instruction" in params
        assert "messages" in params
        assert sig.return_annotation == LLMResponse


class TestLLMServiceMockImplementation:
    @pytest.fixture
    def mock_service(self):
        return AsyncMock(spec=LLMService)

    async def test_generate_content(self, mock_service):
        response = MagicMock(spec=LLMResponse)
        mock_service.generate_content.return_value = response
        result = await mock_service.generate_content(
            model_name="flash", system_instruction="be helpful", messages=[]
        )
        assert result is response

    def test_supports_caching(self, mock_service):
        mock_service.supports_caching.return_value = False
        assert mock_service.supports_caching() is False

    def test_get_capabilities(self, mock_service):
        caps = MagicMock()
        mock_service.get_capabilities.return_value = caps
        assert mock_service.get_capabilities() is caps


# =============================================================================
# LogSink (Protocol)
# =============================================================================

class TestLogSinkContract:
    """LogSink uses Protocol, not ABC — no TypeError on direct instantiation."""

    def test_has_log_method(self):
        assert hasattr(LogSink, "log")

    def test_log_signature(self):
        sig = inspect.signature(LogSink.log)
        params = list(sig.parameters.keys())
        assert params == ["self", "entry"]

    def test_log_is_sync(self):
        assert not inspect.iscoroutinefunction(LogSink.log)

    def test_mock_satisfies_protocol(self):
        """A MagicMock with a log method satisfies the Protocol."""
        sink = MagicMock()
        sink.log({"level": "info", "msg": "test"})
        sink.log.assert_called_once()


# =============================================================================
# PlatformAuthPort
# =============================================================================

class TestPlatformAuthPortContract:
    def test_is_abstract_class(self):
        assert issubclass(PlatformAuthPort, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            PlatformAuthPort()

    def test_has_authorize(self):
        assert getattr(PlatformAuthPort.authorize, "__isabstractmethod__", False)

    def test_all_abstract_methods_count(self):
        abstract_methods = {
            name for name, method in inspect.getmembers(PlatformAuthPort)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 1, f"Expected 1 abstract method, got {abstract_methods}"

    def test_authorize_signature(self):
        sig = inspect.signature(PlatformAuthPort.authorize)
        params = list(sig.parameters.keys())
        assert params == ["self", "platform", "platform_user_id", "email"]
        assert sig.parameters["platform_user_id"].default is None
        assert sig.parameters["email"].default is None


class TestPlatformAuthPortMockImplementation:
    @pytest.fixture
    def mock_port(self):
        return AsyncMock(spec=PlatformAuthPort)

    async def test_authorize_returns_decision(self, mock_port):
        decision = IAMDecision(action="allow")
        mock_port.authorize.return_value = decision
        result = await mock_port.authorize("slack", platform_user_id="U123")
        assert result.action == "allow"


# =============================================================================
# QuotaService
# =============================================================================

class TestQuotaServiceContract:
    def test_is_abstract_class(self):
        assert issubclass(QuotaService, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            QuotaService()

    def test_has_record_usage(self):
        assert getattr(QuotaService.record_usage, "__isabstractmethod__", False)

    def test_all_abstract_methods_count(self):
        abstract_methods = {
            name for name, method in inspect.getmembers(QuotaService)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 1, f"Expected 1 abstract method, got {abstract_methods}"

    def test_record_usage_signature(self):
        sig = inspect.signature(QuotaService.record_usage)
        params = list(sig.parameters.keys())
        assert params == ["self", "user_id", "model", "tokens", "cost"]


class TestQuotaServiceMockImplementation:
    @pytest.fixture
    def mock_service(self):
        return AsyncMock(spec=QuotaService)

    async def test_record_usage(self, mock_service):
        await mock_service.record_usage("u1", "gemini-flash", 500, 0.001)
        mock_service.record_usage.assert_called_once_with("u1", "gemini-flash", 500, 0.001)


# =============================================================================
# SearchEnrichmentPort
# =============================================================================

class TestSearchEnrichmentPortContract:
    def test_is_abstract_class(self):
        assert issubclass(SearchEnrichmentPort, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            SearchEnrichmentPort()

    def test_has_enrich_context(self):
        assert getattr(SearchEnrichmentPort.enrich_context, "__isabstractmethod__", False)

    def test_all_abstract_methods_count(self):
        abstract_methods = {
            name for name, method in inspect.getmembers(SearchEnrichmentPort)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 1, f"Expected 1 abstract method, got {abstract_methods}"

    def test_enrich_context_signature(self):
        sig = inspect.signature(SearchEnrichmentPort.enrich_context)
        params = list(sig.parameters.keys())
        assert "keywords" in params
        assert "search_phrase_1" in params
        assert "search_phrase_2" in params
        assert sig.parameters["dedup_threshold"].default == 0.98
        assert sig.parameters["skip_semantic_dedup"].default is False


class TestSearchEnrichmentPortMockImplementation:
    @pytest.fixture
    def mock_port(self):
        return AsyncMock(spec=SearchEnrichmentPort)

    async def test_enrich_context(self, mock_port):
        ctx = MagicMock()
        mock_port.enrich_context.return_value = ctx
        result = await mock_port.enrich_context(["kw"], "phrase1", "phrase2")
        assert result is ctx


# =============================================================================
# TaskQueue (Protocol)
# =============================================================================

class TestTaskQueueContract:
    """TaskQueue uses Protocol, not ABC — no TypeError on direct instantiation."""

    def test_has_enqueue_slack_event(self):
        assert hasattr(TaskQueue, "enqueue_slack_event")

    def test_has_create_queue_if_not_exists(self):
        assert hasattr(TaskQueue, "create_queue_if_not_exists")

    def test_has_get_queue_stats(self):
        assert hasattr(TaskQueue, "get_queue_stats")

    def test_has_purge_queue(self):
        assert hasattr(TaskQueue, "purge_queue")

    def test_enqueue_slack_event_is_async(self):
        assert inspect.iscoroutinefunction(TaskQueue.enqueue_slack_event)

    def test_get_queue_stats_is_sync(self):
        assert not inspect.iscoroutinefunction(TaskQueue.get_queue_stats)

    def test_enqueue_slack_event_signature(self):
        sig = inspect.signature(TaskQueue.enqueue_slack_event)
        params = list(sig.parameters.keys())
        assert params == ["self", "event_data", "session_id", "delay_seconds", "trace_headers"]
        assert sig.parameters["delay_seconds"].default == 0
        assert sig.parameters["trace_headers"].default is None


class TestTaskQueueMockImplementation:
    @pytest.fixture
    def mock_queue(self):
        return AsyncMock(spec=TaskQueue)

    async def test_enqueue_slack_event(self, mock_queue):
        mock_queue.enqueue_slack_event.return_value = "task-id-123"
        result = await mock_queue.enqueue_slack_event({"type": "message"}, "session1")
        assert isinstance(result, str)

    async def test_create_queue_if_not_exists(self, mock_queue):
        await mock_queue.create_queue_if_not_exists()
        mock_queue.create_queue_if_not_exists.assert_called_once()

    async def test_purge_queue(self, mock_queue):
        await mock_queue.purge_queue()
        mock_queue.purge_queue.assert_called_once()


# =============================================================================
# UserRepository
# =============================================================================

class TestUserRepositoryContract:
    def test_is_abstract_class(self):
        assert issubclass(UserRepository, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            UserRepository()

    def test_has_get_user(self):
        assert getattr(UserRepository.get_user, "__isabstractmethod__", False)

    def test_has_get_user_by_platform_id(self):
        assert getattr(UserRepository.get_user_by_platform_id, "__isabstractmethod__", False)

    def test_has_get_user_by_email(self):
        assert getattr(UserRepository.get_user_by_email, "__isabstractmethod__", False)

    def test_has_get_user_by_external_id(self):
        assert getattr(UserRepository.get_user_by_external_id, "__isabstractmethod__", False)

    def test_has_link_platform_identity(self):
        assert getattr(UserRepository.link_platform_identity, "__isabstractmethod__", False)

    def test_has_unlink_platform_identity(self):
        assert getattr(UserRepository.unlink_platform_identity, "__isabstractmethod__", False)

    def test_has_create_user(self):
        assert getattr(UserRepository.create_user, "__isabstractmethod__", False)

    def test_has_update_user(self):
        assert getattr(UserRepository.update_user, "__isabstractmethod__", False)

    def test_has_delete_user(self):
        assert getattr(UserRepository.delete_user, "__isabstractmethod__", False)

    def test_has_increment_usage(self):
        assert getattr(UserRepository.increment_usage, "__isabstractmethod__", False)

    def test_convenience_aliases_are_not_abstract(self):
        """add_platform_id and remove_platform_id are convenience aliases, NOT abstract."""
        assert not getattr(UserRepository.add_platform_id, "__isabstractmethod__", False)
        assert not getattr(UserRepository.remove_platform_id, "__isabstractmethod__", False)

    def test_all_abstract_methods_count(self):
        """Port should have exactly 10 abstract methods (aliases excluded)."""
        abstract_methods = {
            name for name, method in inspect.getmembers(UserRepository)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 10, f"Expected 10 abstract methods, got {abstract_methods}"


class TestUserRepositoryMockImplementation:
    @pytest.fixture
    def mock_repo(self):
        return AsyncMock(spec=UserRepository)

    async def test_get_user_not_found(self, mock_repo):
        mock_repo.get_user.return_value = None
        result = await mock_repo.get_user("unknown")
        assert result is None

    async def test_get_user_by_platform_id(self, mock_repo):
        user = MagicMock()
        mock_repo.get_user_by_platform_id.return_value = user
        result = await mock_repo.get_user_by_platform_id("slack", "U123")
        assert result is user

    async def test_create_user(self, mock_repo):
        user = MagicMock()
        mock_repo.create_user.return_value = user
        result = await mock_repo.create_user(user)
        assert result is user

    async def test_delete_user_returns_bool(self, mock_repo):
        mock_repo.delete_user.return_value = True
        result = await mock_repo.delete_user("u1")
        assert result is True

    async def test_increment_usage(self, mock_repo):
        await mock_repo.increment_usage("u1", 200, 0.002)
        mock_repo.increment_usage.assert_called_once_with("u1", 200, 0.002)


# =============================================================================
# WhitelistRepository
# =============================================================================

class TestWhitelistRepositoryContract:
    def test_is_abstract_class(self):
        assert issubclass(WhitelistRepository, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            WhitelistRepository()

    def test_has_get_whitelist(self):
        assert getattr(WhitelistRepository.get_whitelist, "__isabstractmethod__", False)

    def test_has_add_email(self):
        assert getattr(WhitelistRepository.add_email, "__isabstractmethod__", False)

    def test_has_remove_email(self):
        assert getattr(WhitelistRepository.remove_email, "__isabstractmethod__", False)

    def test_has_add_domain(self):
        assert getattr(WhitelistRepository.add_domain, "__isabstractmethod__", False)

    def test_has_remove_domain(self):
        assert getattr(WhitelistRepository.remove_domain, "__isabstractmethod__", False)

    def test_all_abstract_methods_count(self):
        abstract_methods = {
            name for name, method in inspect.getmembers(WhitelistRepository)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 5, f"Expected 5 abstract methods, got {abstract_methods}"


class TestWhitelistRepositoryMockImplementation:
    @pytest.fixture
    def mock_repo(self):
        return AsyncMock(spec=WhitelistRepository)

    async def test_get_whitelist(self, mock_repo):
        wl = MagicMock()
        mock_repo.get_whitelist.return_value = wl
        result = await mock_repo.get_whitelist()
        assert result is wl

    async def test_add_email(self, mock_repo):
        await mock_repo.add_email("user@example.com")
        mock_repo.add_email.assert_called_once_with("user@example.com")

    async def test_remove_domain(self, mock_repo):
        await mock_repo.remove_domain("example.com")
        mock_repo.remove_domain.assert_called_once_with("example.com")
