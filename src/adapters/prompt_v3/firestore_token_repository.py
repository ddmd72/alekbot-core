"""
FirestoreTokenRepository - Firestore adapter for token storage.

Part of Prompt Design System v3 (RFC).
"""

import logging
from typing import List

from google.cloud import firestore

from src.ports.prompt_v3.token_repository import TokenRepository
from src.domain.prompt_v3.token import Token, TokenId, TokenCategory, TokenClass
from src.domain.prompt_v3.security import SecurityPort

logger = logging.getLogger(__name__)


class FirestoreTokenRepository(TokenRepository):
    """Firestore adapter for token storage.

    Data Model:
        {
            "token_id": "HUMOR_PRESET_RANEVSKAYA",
            "category": "humor_engine",
            "class": "properties",
            "content": "humor_engine { ... }",  # Groovy code block
            "metadata": {
                "version": "1.0",
                "author": "system",
                "description": "Ranevskaya humor style",
                "validation": {...}  # Added by Token.create()
            }
        }

    Examples:
        >>> from google.cloud import firestore
        >>> from src.adapters.security.regex_adapter import RegexSecurityAdapter
        >>>
        >>> db = firestore.Client()
        >>> security_port = RegexSecurityAdapter()
        >>> repo = FirestoreTokenRepository(
        ...     db, "dev_prompt_tokens_v3", security_port
        ... )
        >>> token = await repo.get(TokenId("HUMOR_PRESET_OFF"))
    """

    def __init__(
        self,
        db: firestore.Client,
        system_collection: str,
        user_collection: str,
        security_port: SecurityPort
    ):
        """Initialize Firestore token repository with dual-collection support.

        Args:
            db: Firestore client instance
            system_collection: System tokens collection (e.g., "dev_prompt_system_tokens")
            user_collection: User tokens collection (e.g., "dev_prompt_user_tokens")
            security_port: SecurityPort for token validation
        """
        self.db = db
        self.system_collection = system_collection
        self.user_collection = user_collection
        self.security_port = security_port

    async def _iterate_docs(self, query):
        """Iterate Firestore query results for sync or async clients."""
        docs = query.stream()
        if hasattr(docs, "__aiter__"):
            async for doc in docs:
                yield doc
        else:
            for doc in docs:
                yield doc

    async def get(self, token_id: TokenId) -> Token:
        """Fetch token by ID with dual-collection lookup.

        Looks up in system collection first, then fallback to user collection.
        Validates token content using SecurityPort via Token.create().

        Args:
            token_id: Unique token identifier

        Returns:
            Token instance

        Raises:
            KeyError: If token not found in either collection
        """
        # Try system collection first
        doc_ref = self.db.collection(self.system_collection).document(str(token_id))
        doc = await doc_ref.get()

        # Fallback to user collection
        if not doc.exists:
            doc_ref = self.db.collection(self.user_collection).document(str(token_id))
            doc = await doc_ref.get()

        if not doc.exists:
            raise KeyError(f"Token not found: {token_id}")

        data = doc.to_dict()

        # Use Token.create() for validation (GAP 1 resolution)
        token_id_value = data.get("token_id")
        if not token_id_value:
            raise KeyError("Token document missing token_id field")
        token = await Token.create(
            id=TokenId(token_id_value),
            category=TokenCategory(data["category"]),
            class_=TokenClass(data["class"]),
            content=data["content"],
            metadata=data.get("metadata", {}),
            security_port=self.security_port
        )

        return token

    async def list_by_category(
        self,
        category: TokenCategory,
        scope: str = "all"
    ) -> List[Token]:
        """List all tokens in category with scope control.

        Args:
            category: Token category (e.g., "humor_engine")
            scope: Search scope - "system", "user", or "all" (default: "all")

        Returns:
            List of tokens in category (may be empty)
        """
        tokens = []

        # Search system collection
        if scope in ["system", "all"]:
            query = (
                self.db.collection(self.system_collection)
                .where("category", "==", str(category))
            )
            async for doc in self._iterate_docs(query):
                data = doc.to_dict()
                token_id_value = data.get("token_id")
                if not token_id_value:
                    raise KeyError("Token document missing token_id field")
                token = await Token.create(
                    id=TokenId(token_id_value),
                    category=TokenCategory(data["category"]),
                    class_=TokenClass(data["class"]),
                    content=data["content"],
                    metadata=data.get("metadata", {}),
                    security_port=self.security_port
                )
                tokens.append(token)

        # Search user collection
        if scope in ["user", "all"]:
            query = (
                self.db.collection(self.user_collection)
                .where("category", "==", str(category))
            )
            async for doc in self._iterate_docs(query):
                data = doc.to_dict()
                token_id_value = data.get("token_id")
                if not token_id_value:
                    raise KeyError("Token document missing token_id field")
                token = await Token.create(
                    id=TokenId(token_id_value),
                    category=TokenCategory(data["category"]),
                    class_=TokenClass(data["class"]),
                    content=data["content"],
                    metadata=data.get("metadata", {}),
                    security_port=self.security_port
                )
                tokens.append(token)

        return tokens

    async def list_by_class(
        self,
        token_class: TokenClass,
        scope: str = "all"
    ) -> List[Token]:
        """List all tokens in class with scope control.

        Args:
            token_class: Token class (e.g., "properties")
            scope: Search scope - "system", "user", or "all" (default: "all")

        Returns:
            List of tokens in class (may be empty)
        """
        tokens = []

        # Search system collection
        if scope in ["system", "all"]:
            query = (
                self.db.collection(self.system_collection)
                .where("class", "==", str(token_class))
            )
            async for doc in self._iterate_docs(query):
                data = doc.to_dict()
                token_id_value = data.get("token_id")
                if not token_id_value:
                    raise KeyError("Token document missing token_id field")
                token = await Token.create(
                    id=TokenId(token_id_value),
                    category=TokenCategory(data["category"]),
                    class_=TokenClass(data["class"]),
                    content=data["content"],
                    metadata=data.get("metadata", {}),
                    security_port=self.security_port
                )
                tokens.append(token)

        # Search user collection
        if scope in ["user", "all"]:
            query = (
                self.db.collection(self.user_collection)
                .where("class", "==", str(token_class))
            )
            async for doc in self._iterate_docs(query):
                data = doc.to_dict()
                token_id_value = data.get("token_id")
                if not token_id_value:
                    raise KeyError("Token document missing token_id field")
                token = await Token.create(
                    id=TokenId(token_id_value),
                    category=TokenCategory(data["category"]),
                    class_=TokenClass(data["class"]),
                    content=data["content"],
                    metadata=data.get("metadata", {}),
                    security_port=self.security_port
                )
                tokens.append(token)

        return tokens

    async def list_all(self, scope: str = "all") -> List[Token]:
        """List all tokens with scope control.

        Args:
            scope: Search scope - "system", "user", or "all" (default: "all")

        Returns:
            List of all tokens in specified scope
        """
        tokens = []

        # List from system collection
        if scope in ["system", "all"]:
            query = self.db.collection(self.system_collection)
            async for doc in self._iterate_docs(query):
                data = doc.to_dict()
                token_id_value = data.get("token_id")
                if not token_id_value:
                    raise KeyError("Token document missing token_id field")
                token = await Token.create(
                    id=TokenId(token_id_value),
                    category=TokenCategory(data["category"]),
                    class_=TokenClass(data["class"]),
                    content=data["content"],
                    metadata=data.get("metadata", {}),
                    security_port=self.security_port
                )
                tokens.append(token)

        # List from user collection
        if scope in ["user", "all"]:
            query = self.db.collection(self.user_collection)
            async for doc in self._iterate_docs(query):
                data = doc.to_dict()
                token_id_value = data.get("token_id")
                if not token_id_value:
                    raise KeyError("Token document missing token_id field")
                token = await Token.create(
                    id=TokenId(token_id_value),
                    category=TokenCategory(data["category"]),
                    class_=TokenClass(data["class"]),
                    content=data["content"],
                    metadata=data.get("metadata", {}),
                    security_port=self.security_port
                )
                tokens.append(token)

        return tokens

    async def save(self, token: Token, collection: str = "system") -> None:
        """Save token to specified collection.

        Args:
            token: Token instance to save (already validated)
            collection: Target collection - "system" or "user" (default: "system")
        """
        if collection == "system":
            collection_name = self.system_collection
        elif collection == "user":
            collection_name = self.user_collection
        else:
            raise ValueError(f"Invalid collection: {collection}. Must be 'system' or 'user'")

        doc_ref = self.db.collection(collection_name).document(str(token.id))

        data = {
            "token_id": str(token.id),
            "category": str(token.category),
            "class": str(token.class_),
            "content": token.content,
            "metadata": token.metadata,
            "created_at": firestore.SERVER_TIMESTAMP,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }

        doc_ref.set(data)
        logger.info(f"Saved token: {token.id} to {collection} collection")

    async def delete(self, token_id: TokenId) -> None:
        """Delete token from repository (checks both collections).

        Args:
            token_id: Token ID to delete

        Raises:
            KeyError: If token not found in either collection
        """
        # Try system collection first
        doc_ref = self.db.collection(self.system_collection).document(str(token_id))
        doc = await doc_ref.get()
        if doc.exists:
            doc_ref.delete()
            logger.info(f"Deleted token: {token_id} from system collection")
            return

        # Try user collection
        doc_ref = self.db.collection(self.user_collection).document(str(token_id))
        doc = await doc_ref.get()
        if doc.exists:
            doc_ref.delete()
            logger.info(f"Deleted token: {token_id} from user collection")
            return

        raise KeyError(f"Token not found: {token_id}")

    async def exists(self, token_id: TokenId) -> bool:
        """Check if token exists in either collection.

        Args:
            token_id: Token ID to check

        Returns:
            True if token exists in system or user collection, False otherwise
        """
        # Check system collection
        doc_ref = self.db.collection(self.system_collection).document(str(token_id))
        doc = await doc_ref.get()
        if doc.exists:
            return True

        # Check user collection
        doc_ref = self.db.collection(self.user_collection).document(str(token_id))
        doc = await doc_ref.get()
        return doc.exists
