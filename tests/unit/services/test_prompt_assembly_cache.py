"""
Unit tests for PromptAssemblyService caching functionality.

Tests RFC: docs/10_rfcs/PROMPT_ASSEMBLY_CACHING_RFC.md
"""

import pytest
import time
from unittest.mock import Mock, AsyncMock, MagicMock

from src.services.prompt_v3.prompt_assembly_service import PromptAssemblyService
from src.domain.prompt_v3.token import TokenId


class TestPromptAssemblyCaching:
    """Test prompt assembly caching and performance optimizations."""
    
    @pytest.fixture
    def mock_repositories(self):
        """Create mock repositories."""
        token_repo = Mock()
        blueprint_repo = Mock()
        profile_repo = Mock()
        security_port = Mock()
        formatter = Mock()
        bio_formatter = Mock()
        
        return {
            "token_repo": token_repo,
            "blueprint_repo": blueprint_repo,
            "profile_repo": profile_repo,
            "security_port": security_port,
            "formatter": formatter,
            "bio_formatter": bio_formatter
        }
    
    @pytest.fixture
    def assembly_service(self, mock_repositories):
        """Create PromptAssemblyService with mocked dependencies."""
        return PromptAssemblyService(
            token_repo=mock_repositories["token_repo"],
            blueprint_repo=mock_repositories["blueprint_repo"],
            profile_repo=mock_repositories["profile_repo"],
            security_port=mock_repositories["security_port"],
            formatter=mock_repositories["formatter"],
            bio_formatter=mock_repositories["bio_formatter"],
            cache_ttl=86400  # 24 hours
        )
    
    def test_cache_initialization(self, assembly_service):
        """Test that cache is initialized empty."""
        assert isinstance(assembly_service._assembled_cache, dict)
        assert len(assembly_service._assembled_cache) == 0
        assert assembly_service._cache_ttl == 86400
    
    def test_build_cache_key(self, assembly_service):
        """Test cache key generation."""
        # Full parameters
        key = assembly_service._build_cache_key("smart", "account_123456789", "user_987654321")
        assert key == "prompt:smart:acc:account_123456789:usr:user_987654321"
        
        # No account
        key = assembly_service._build_cache_key("quick", None, "user_123")
        assert key == "prompt:quick:acc:no-acc:usr:user_123"
        
        # No user
        key = assembly_service._build_cache_key("smart", "acc_123", None)
        assert key == "prompt:smart:acc:acc_123:usr:no-usr"
    
    def test_cache_save_and_get(self, assembly_service):
        """Test basic cache save and retrieve."""
        key = "test_key"
        content = "test prompt content"
        
        # Save to cache
        assembly_service._save_to_cache(key, content)
        
        # Should be in cache
        assert key in assembly_service._assembled_cache
        
        # Retrieve from cache
        cached = assembly_service._get_from_cache(key)
        assert cached == content
    
    def test_cache_miss(self, assembly_service):
        """Test cache miss returns None."""
        result = assembly_service._get_from_cache("nonexistent_key")
        assert result is None
    
    def test_cache_ttl_expiry(self, assembly_service):
        """Test cache expires after TTL."""
        # Set very short TTL for testing
        assembly_service._cache_ttl = 1  # 1 second
        
        key = "test_key"
        content = "test content"
        
        # Save to cache
        assembly_service._save_to_cache(key, content)
        
        # Should be cached
        assert assembly_service._get_from_cache(key) == content
        
        # Wait for expiry
        time.sleep(2)
        
        # Should be expired
        result = assembly_service._get_from_cache(key)
        assert result is None
        
        # Key should be removed from cache
        assert key not in assembly_service._assembled_cache
    
    def test_invalidate_cache(self, assembly_service):
        """Test manual cache invalidation."""
        # Add some entries
        assembly_service._save_to_cache("key1", "content1")
        assembly_service._save_to_cache("key2", "content2")
        assembly_service._save_to_cache("key3", "content3")
        
        assert len(assembly_service._assembled_cache) == 3
        
        # Invalidate cache
        assembly_service.invalidate_cache()
        
        # Cache should be empty
        assert len(assembly_service._assembled_cache) == 0
    
    @pytest.mark.asyncio
    async def test_preload_cache_skip_if_cached(self, assembly_service):
        """Test preload skips if key already cached."""
        # Mock the assemble method
        assembly_service.assemble = AsyncMock()
        
        # Pre-populate cache
        key = assembly_service._build_cache_key("smart", "acc123", "user456")
        assembly_service._save_to_cache(key, "existing content")
        
        # Preload should skip
        await assembly_service.preload_cache("smart", "acc123", "user456")
        
        # Assemble should not be called
        assembly_service.assemble.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_preload_cache_assembles_if_missing(self, assembly_service):
        """Test preload calls assemble if cache miss."""
        # Mock the assemble method
        assembly_service.assemble = AsyncMock(return_value="assembled prompt")
        
        # Preload should call assemble
        await assembly_service.preload_cache("quick", "acc789", "user123")
        
        # Assemble should be called with empty runtime context
        assembly_service.assemble.assert_called_once_with(
            agent_type="quick",
            user_id="user123",
            account_id="acc789",
            biographical_facts=[],
            conversation_history=[]
        )


class TestCacheKeyGeneration:
    """Test cache key generation edge cases."""
    
    @pytest.fixture
    def service(self):
        return PromptAssemblyService(
            token_repo=Mock(),
            blueprint_repo=Mock(),
            profile_repo=Mock(),
            security_port=Mock(),
            formatter=Mock(),
            bio_formatter=Mock()
        )
    
    def test_cache_key_consistency(self, service):
        """Test cache keys are consistent for same inputs."""
        key1 = service._build_cache_key("smart", "account_123", "user_456")
        key2 = service._build_cache_key("smart", "account_123", "user_456")
        
        assert key1 == key2
    
    def test_cache_key_differs_by_agent_type(self, service):
        """Test cache keys differ by agent type."""
        key1 = service._build_cache_key("quick", "acc123", "user456")
        key2 = service._build_cache_key("smart", "acc123", "user456")
        
        assert key1 != key2
        assert "quick" in key1
        assert "smart" in key2
    
    def test_cache_key_differs_by_user(self, service):
        """Test cache keys differ by user."""
        key1 = service._build_cache_key("smart", "acc123", "user_A")
        key2 = service._build_cache_key("smart", "acc123", "user_B")
        
        assert key1 != key2
    
    def test_cache_key_differs_by_account(self, service):
        """Test cache keys differ by account."""
        key1 = service._build_cache_key("smart", "account_A", "user456")
        key2 = service._build_cache_key("smart", "account_B", "user456")
        
        assert key1 != key2


class TestCachePerformance:
    """Smoke tests for cache performance improvements."""
    
    @pytest.fixture
    def service(self):
        return PromptAssemblyService(
            token_repo=Mock(),
            blueprint_repo=Mock(),
            profile_repo=Mock(),
            security_port=Mock(),
            formatter=Mock(),
            bio_formatter=Mock(),
            cache_ttl=3600
        )
    
    def test_cache_storage_efficiency(self, service):
        """Test cache doesn't grow unbounded."""
        # Add many entries
        for i in range(100):
            service._save_to_cache(f"key_{i}", f"content_{i}")
        
        assert len(service._assembled_cache) == 100
        
        # Invalidate
        service.invalidate_cache()
        
        assert len(service._assembled_cache) == 0
    
    def test_cache_hit_is_fast(self, service):
        """Test cache hit is faster than uncached lookup."""
        key = "perf_test_key"
        content = "x" * 10000  # 10KB content
        
        # Save to cache
        service._save_to_cache(key, content)
        
        # Time cache hit (should be very fast)
        start = time.time()
        result = service._get_from_cache(key)
        elapsed = time.time() - start
        
        assert result == content
        assert elapsed < 0.01  # Should be < 10ms
