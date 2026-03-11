"""
Repository port (interface) for whitelist data.

Part of Hexagonal Architecture - defines contract for whitelist storage.
Allows swapping implementations (Firestore, YAML, PostgreSQL, etc).
"""
from abc import ABC, abstractmethod
from ..domain.whitelist import WhitelistEntry


class WhitelistRepository(ABC):
    """
    Repository interface for managing whitelist configuration.
    
    This is a Port in Hexagonal Architecture - defines the contract
    for whitelist storage without specifying implementation details.
    
    Implementations (Adapters):
    - FirestoreWhitelistRepository (production)
    - YamlWhitelistRepository (local dev)
    - MockWhitelistRepository (testing)
    """
    
    @abstractmethod
    async def get_whitelist(self) -> WhitelistEntry:
        """
        Retrieve current whitelist configuration.
        
        Returns:
            WhitelistEntry with allowed emails and domains
            
        Raises:
            Exception: If whitelist cannot be loaded
            
        Note:
            Should return empty whitelist (no emails/domains) if not configured,
            rather than raising an exception.
        """
        pass
    
    @abstractmethod
    async def add_email(self, email: str) -> None:
        """
        Add email to whitelist.
        
        Args:
            email: Email address to add
            
        Raises:
            ValueError: If email format is invalid
            Exception: If operation fails
            
        Note:
            Should be idempotent - adding existing email is not an error.
        """
        pass
    
    @abstractmethod
    async def remove_email(self, email: str) -> None:
        """
        Remove email from whitelist.
        
        Args:
            email: Email address to remove
            
        Raises:
            Exception: If operation fails
            
        Note:
            Should be idempotent - removing non-existent email is not an error.
        """
        pass
    
    @abstractmethod
    async def add_domain(self, domain: str) -> None:
        """
        Add domain to whitelist.
        
        Args:
            domain: Domain to add (e.g., "company.com")
            
        Raises:
            ValueError: If domain format is invalid
            Exception: If operation fails
            
        Note:
            Should be idempotent - adding existing domain is not an error.
        """
        pass
    
    @abstractmethod
    async def remove_domain(self, domain: str) -> None:
        """
        Remove domain from whitelist.
        
        Args:
            domain: Domain to remove
            
        Raises:
            Exception: If operation fails
            
        Note:
            Should be idempotent - removing non-existent domain is not an error.
        """
        pass
