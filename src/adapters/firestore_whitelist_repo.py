"""
Firestore implementation of WhitelistRepository.

Stores whitelist configuration in Firestore with atomic operations.
Swappable implementation - can be replaced with YAML, PostgreSQL, etc.
"""
from google.cloud import firestore

from ..domain.whitelist import WhitelistEntry
from ..ports.whitelist_repository import WhitelistRepository
from ..config.environment import EnvironmentConfig
from ..utils.logger import logger


class FirestoreWhitelistRepository(WhitelistRepository):
    """
    Firestore adapter for whitelist storage.
    
    Storage Schema:
    - Collection: {env}_whitelist (e.g., "dev_whitelist", "prod_whitelist")
    - Document: "config" (singleton document)
    - Fields:
        - allowed_emails: List[str] (array of email addresses)
        - allowed_domains: List[str] (array of domains)
        - updated_at: Timestamp (server timestamp)
    
    Example Document:
    ```
    {
        "allowed_emails": ["user@example.com"],
        "allowed_domains": ["example.com"],
        "updated_at": Timestamp(2026, 2, 5, 18, 0, 0)
    }
    ```
    
    Implementation Notes:
    - Uses firestore.ArrayUnion/ArrayRemove for atomic updates
    - No caching (always reads fresh from DB for security)
    - Idempotent operations (adding existing email is not an error)
    - Returns empty whitelist if document doesn't exist (secure default)
    """
    
    def __init__(self, db_client, env_config: EnvironmentConfig):
        """
        Initialize Firestore whitelist repository.
        
        Args:
            db_client: Firestore client instance
            env_config: Environment configuration (for collection naming)
        """
        self.db = db_client
        self.env_config = env_config
        
        # ADR-006: Use semantic collection naming
        # Dev: development_domain_whitelist_v1
        # Prod: domain_whitelist_v1
        collection_name = env_config.domain_whitelist_collection
        self.collection = self.db.collection(collection_name)
        self.doc_id = "config"  # Singleton document
        
        logger.info(f"📂 Whitelist Repository initialized. Collection: {collection_name}")
    
    async def get_whitelist(self) -> WhitelistEntry:
        """
        Retrieve whitelist configuration from Firestore.
        
        Returns:
            WhitelistEntry with allowed emails and domains
            
        Note:
            Returns empty whitelist (no access) if document doesn't exist.
            This is secure default - deny all if not configured.
        """
        doc = await self.collection.document(self.doc_id).get()
        
        if not doc.exists:
            logger.warning(
                "⚠️ Whitelist config not found in Firestore. "
                "Returning empty whitelist (no one allowed)."
            )
            # Secure default: empty whitelist = no one allowed
            return WhitelistEntry(
                allowed_emails=set(),
                allowed_domains=set()
            )
        
        data = doc.to_dict()
        
        # Convert lists to sets (domain model uses sets)
        allowed_emails = set(data.get("allowed_emails", []))
        allowed_domains = set(data.get("allowed_domains", []))
        
        logger.debug(
            f"📋 Whitelist loaded: "
            f"{len(allowed_emails)} emails, {len(allowed_domains)} domains"
        )
        
        return WhitelistEntry(
            allowed_emails=allowed_emails,
            allowed_domains=allowed_domains
        )
    
    async def add_email(self, email: str) -> None:
        """
        Add email to whitelist using atomic Firestore operation.
        
        Args:
            email: Email address to add (will be normalized to lowercase)
            
        Note:
            Idempotent - adding existing email is not an error.
            Uses ArrayUnion for atomic operation (safe for concurrent writes).
        """
        email = email.lower().strip()
        
        # Firestore ArrayUnion adds element only if not already present
        await self.collection.document(self.doc_id).set(
            {
                "allowed_emails": firestore.ArrayUnion([email]),
                "updated_at": firestore.SERVER_TIMESTAMP
            },
            merge=True  # Don't overwrite other fields
        )
        
        logger.info(f"✅ Email added to whitelist: {email}")
    
    async def remove_email(self, email: str) -> None:
        """
        Remove email from whitelist using atomic Firestore operation.
        
        Args:
            email: Email address to remove (will be normalized to lowercase)
            
        Note:
            Idempotent - removing non-existent email is not an error.
            Uses ArrayRemove for atomic operation.
        """
        email = email.lower().strip()
        
        # Firestore ArrayRemove removes element if present
        await self.collection.document(self.doc_id).set(
            {
                "allowed_emails": firestore.ArrayRemove([email]),
                "updated_at": firestore.SERVER_TIMESTAMP
            },
            merge=True
        )
        
        logger.info(f"🗑️ Email removed from whitelist: {email}")
    
    async def add_domain(self, domain: str) -> None:
        """
        Add domain to whitelist using atomic Firestore operation.
        
        Args:
            domain: Domain to add (e.g., "company.com", will be normalized)
            
        Note:
            Idempotent - adding existing domain is not an error.
            Uses ArrayUnion for atomic operation.
        """
        domain = domain.lower().strip()
        
        await self.collection.document(self.doc_id).set(
            {
                "allowed_domains": firestore.ArrayUnion([domain]),
                "updated_at": firestore.SERVER_TIMESTAMP
            },
            merge=True
        )
        
        logger.info(f"✅ Domain added to whitelist: {domain}")
    
    async def remove_domain(self, domain: str) -> None:
        """
        Remove domain from whitelist using atomic Firestore operation.
        
        Args:
            domain: Domain to remove (will be normalized to lowercase)
            
        Note:
            Idempotent - removing non-existent domain is not an error.
            Uses ArrayRemove for atomic operation.
        """
        domain = domain.lower().strip()
        
        await self.collection.document(self.doc_id).set(
            {
                "allowed_domains": firestore.ArrayRemove([domain]),
                "updated_at": firestore.SERVER_TIMESTAMP
            },
            merge=True
        )
        
        logger.info(f"🗑️ Domain removed from whitelist: {domain}")
