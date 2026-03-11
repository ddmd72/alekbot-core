"""
Domain entities for whitelist functionality.

Whitelist controls who can register (OAuth) and join teams (Team Invites).
Pure domain logic - no infrastructure dependencies.
"""
from typing import Set
from dataclasses import dataclass


@dataclass
class WhitelistEntry:
    """
    Domain entity representing whitelist configuration.
    
    Immutable configuration that determines who can access the system.
    Used by IAMService for authorization decisions.
    """
    allowed_emails: Set[str]
    allowed_domains: Set[str]
    
    def is_allowed(self, email: str) -> bool:
        """
        Check if email passes whitelist validation.
        
        Logic:
        1. Exact email match (case-insensitive)
        2. Domain match (case-insensitive)
        
        Args:
            email: Email address to validate
            
        Returns:
            True if email is whitelisted, False otherwise
            
        Example:
            >>> whitelist = WhitelistEntry(
            ...     allowed_emails={"admin@example.com"},
            ...     allowed_domains={"company.com"}
            ... )
            >>> whitelist.is_allowed("admin@example.com")  # Exact match
            True
            >>> whitelist.is_allowed("user@company.com")  # Domain match
            True
            >>> whitelist.is_allowed("random@other.com")  # Not allowed
            False
        """
        email = email.lower().strip()
        
        # 1. Exact email match
        if email in self.allowed_emails:
            return True
        
        # 2. Domain match
        if "@" in email:
            domain = email.split("@")[-1]
            if domain in self.allowed_domains:
                return True
        
        return False
