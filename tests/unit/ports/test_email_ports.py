"""
Port contract tests for Email Indexing System.
See docs/10_rfcs/GMAIL_EMAIL_INDEXING_RFC.md §2.1.2.

Verifies each port:
- Is an ABC
- Cannot be instantiated directly
- Declares all required abstract methods with correct signatures
"""

import inspect
import pytest
from abc import ABC

from src.ports.email_provider_port import EmailProviderPort
from src.ports.oauth_credentials_port import OAuthCredentialsPort
from src.ports.indexed_email_repository import IndexedEmailRepository
from src.ports.email_exclusions_port import EmailExclusionsPort
from src.ports.email_indexing_job_repository import EmailIndexingJobRepository


def _abstract_methods(cls) -> set:
    return {
        name for name, method in inspect.getmembers(cls)
        if getattr(method, "__isabstractmethod__", False)
    }


def _params(cls, method_name: str) -> list:
    return list(inspect.signature(getattr(cls, method_name)).parameters.keys())


# =============================================================================
# EmailProviderPort
# =============================================================================

class TestEmailProviderPort:

    def test_is_abstract(self):
        assert issubclass(EmailProviderPort, ABC)

    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            EmailProviderPort()

    def test_abstract_method_count(self):
        assert _abstract_methods(EmailProviderPort) == {
            "list_emails", "batch_get_full_content", "refresh_token"
        }

    def test_list_emails_signature(self):
        params = _params(EmailProviderPort, "list_emails")
        assert "credentials" in params
        assert "date_from" in params
        assert "page_token" in params
        assert "max_results" in params
        assert "query" in params

    def test_batch_get_full_content_signature(self):
        params = _params(EmailProviderPort, "batch_get_full_content")
        assert "credentials" in params
        assert "email_ids" in params
        assert "deep" in params

    def test_refresh_token_signature(self):
        params = _params(EmailProviderPort, "refresh_token")
        assert "credentials" in params


# =============================================================================
# OAuthCredentialsPort
# =============================================================================

class TestOAuthCredentialsPort:

    def test_is_abstract(self):
        assert issubclass(OAuthCredentialsPort, ABC)

    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            OAuthCredentialsPort()

    def test_abstract_method_count(self):
        assert _abstract_methods(OAuthCredentialsPort) == {
            "get_credentials", "save_credentials", "revoke_credentials",
            "is_connected", "list_connected_providers"
        }

    def test_get_credentials_signature(self):
        params = _params(OAuthCredentialsPort, "get_credentials")
        assert "user_id" in params
        assert "provider" in params

    def test_save_credentials_signature(self):
        params = _params(OAuthCredentialsPort, "save_credentials")
        assert "credentials" in params

    def test_revoke_credentials_signature(self):
        params = _params(OAuthCredentialsPort, "revoke_credentials")
        assert "user_id" in params
        assert "provider" in params

    def test_is_connected_signature(self):
        params = _params(OAuthCredentialsPort, "is_connected")
        assert "user_id" in params
        assert "provider" in params

    def test_list_connected_providers_signature(self):
        params = _params(OAuthCredentialsPort, "list_connected_providers")
        assert "user_id" in params


# =============================================================================
# IndexedEmailRepository
# =============================================================================

class TestIndexedEmailRepository:

    def test_is_abstract(self):
        assert issubclass(IndexedEmailRepository, ABC)

    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            IndexedEmailRepository()

    def test_abstract_method_count(self):
        assert _abstract_methods(IndexedEmailRepository) == {
            "save_batch", "find_nearest", "get_indexing_state",
            "update_indexing_state", "clear_indexing_state", "count_by_user",
            "delete_by_user", "get_unconsolidated_batch", "mark_consolidated",
            "get_pending_embeddings", "update_vectors",
        }

    def test_save_batch_signature(self):
        params = _params(IndexedEmailRepository, "save_batch")
        assert "emails" in params

    def test_find_nearest_signature(self):
        params = _params(IndexedEmailRepository, "find_nearest")
        assert "user_id" in params
        assert "vectors" in params
        assert "limit" in params

    def test_get_unconsolidated_batch_signature(self):
        params = _params(IndexedEmailRepository, "get_unconsolidated_batch")
        assert "user_id" in params
        assert "limit" in params

    def test_mark_consolidated_signature(self):
        params = _params(IndexedEmailRepository, "mark_consolidated")
        assert "email_ids" in params
        assert "consolidated_at" in params

    def test_update_vectors_signature(self):
        params = _params(IndexedEmailRepository, "update_vectors")
        assert "email_id" in params
        assert "vectors" in params


# =============================================================================
# EmailExclusionsPort
# =============================================================================

class TestEmailExclusionsPort:

    def test_is_abstract(self):
        assert issubclass(EmailExclusionsPort, ABC)

    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            EmailExclusionsPort()

    def test_abstract_method_count(self):
        assert _abstract_methods(EmailExclusionsPort) == {
            "get_exclusions", "add_exclusions", "delete_exclusion", "list_exclusions"
        }

    def test_get_exclusions_signature(self):
        params = _params(EmailExclusionsPort, "get_exclusions")
        assert "user_id" in params

    def test_add_exclusions_signature(self):
        params = _params(EmailExclusionsPort, "add_exclusions")
        assert "exclusions" in params

    def test_delete_exclusion_signature(self):
        params = _params(EmailExclusionsPort, "delete_exclusion")
        assert "user_id" in params
        assert "exclusion_id" in params


# =============================================================================
# EmailIndexingJobRepository
# =============================================================================

class TestEmailIndexingJobRepository:

    def test_is_abstract(self):
        assert issubclass(EmailIndexingJobRepository, ABC)

    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            EmailIndexingJobRepository()

    def test_abstract_method_count(self):
        assert _abstract_methods(EmailIndexingJobRepository) == {
            "create_job", "update_job", "get_job", "get_latest_job", "list_jobs"
        }

    def test_create_job_signature(self):
        params = _params(EmailIndexingJobRepository, "create_job")
        assert "job" in params

    def test_update_job_signature(self):
        params = _params(EmailIndexingJobRepository, "update_job")
        assert "job_id" in params
        assert "updates" in params

    def test_get_latest_job_signature(self):
        params = _params(EmailIndexingJobRepository, "get_latest_job")
        assert "user_id" in params
        assert "provider" in params

    def test_list_jobs_signature(self):
        params = _params(EmailIndexingJobRepository, "list_jobs")
        assert "user_id" in params
        assert "limit" in params
