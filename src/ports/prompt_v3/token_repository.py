"""
TokenRepository - Port interface for token storage.

Part of Prompt Design System v3 (RFC).
"""

from abc import ABC, abstractmethod
from typing import List, Optional

from src.domain.prompt_v3.token import Token, TokenId, TokenCategory, TokenClass


class TokenRepository(ABC):
    """Port interface for token storage (hexagonal architecture).

    Implementations:
        - FirestoreTokenRepository: Firestore adapter (Phase 2)
        - CachedTokenRepository: Caching wrapper (Phase 6+, GAP 9 resolution)

    Examples:
        >>> repo = FirestoreTokenRepository(
        ...     db,
        ...     system_collection="dev_prompt_system_tokens",
        ...     user_collection="dev_prompt_user_tokens",
        ...     security_port=security_port
        ... )
        >>> token = await repo.get(TokenId("HUMOR_PRESET_RANEVSKAYA"))
        >>> tokens = await repo.list_by_category(TokenCategory("humor_engine"))
    """

    @abstractmethod
    async def get(self, token_id: TokenId) -> Token:
        """Fetch token by ID.

        Args:
            token_id: Unique token identifier

        Returns:
            Token instance

        Raises:
            KeyError: If token not found

        Examples:
            >>> token = await repo.get(TokenId("HUMOR_PRESET_OFF"))
            >>> assert token.category == TokenCategory("humor_engine")
        """
        pass

    @abstractmethod
    async def list_by_category(self, category: TokenCategory) -> List[Token]:
        """List all tokens in category.

        Args:
            category: Token category (e.g., "humor_engine", "voice")

        Returns:
            List of tokens in category (may be empty)

        Examples:
            >>> tokens = await repo.list_by_category(TokenCategory("humor_engine"))
            >>> assert all(t.category == TokenCategory("humor_engine") for t in tokens)
        """
        pass

    @abstractmethod
    async def list_by_class(self, token_class: TokenClass) -> List[Token]:
        """List all tokens in class.

        Args:
            token_class: Token class (e.g., "properties", "policies")

        Returns:
            List of tokens in class (may be empty)

        Examples:
            >>> tokens = await repo.list_by_class(TokenClass("properties"))
            >>> assert all(t.class_ == TokenClass("properties") for t in tokens)
        """
        pass

    @abstractmethod
    async def list_all(self) -> List[Token]:
        """List all tokens.

        Returns:
            List of all tokens in repository

        Examples:
            >>> all_tokens = await repo.list_all()
            >>> assert len(all_tokens) > 0
        """
        pass

    @abstractmethod
    async def save(self, token: Token) -> None:
        """Save token to repository.

        Args:
            token: Token instance to save

        Examples:
            >>> token = Token(...)
            >>> await repo.save(token)
        """
        pass

    @abstractmethod
    async def delete(self, token_id: TokenId) -> None:
        """Delete token from repository.

        Args:
            token_id: Token ID to delete

        Raises:
            KeyError: If token not found

        Examples:
            >>> await repo.delete(TokenId("OLD_TOKEN"))
        """
        pass

    @abstractmethod
    async def exists(self, token_id: TokenId) -> bool:
        """Check if token exists.

        Args:
            token_id: Token ID to check

        Returns:
            True if token exists, False otherwise

        Examples:
            >>> if await repo.exists(TokenId("HUMOR_PRESET_OFF")):
            ...     token = await repo.get(TokenId("HUMOR_PRESET_OFF"))
        """
        pass
